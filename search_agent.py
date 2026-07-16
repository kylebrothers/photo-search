"""
search_agent.py — Tool-calling search agent (Claude API).

Reference-based tool-calling loop. The agent resolves names/places/dates and
composes retrieval by calling tools, then signals completion with
finalize_search. Retrieval results are held server-side in a per-request
ResultStore; the model passes handles, never bulk photo ids (see tools.py).

Design decisions (see README "Agreed design going forward"):
  - Model pinned via env (AGENT_MODEL), NOT auto-"latest" — SQL/tool
    correctness is prompt-sensitive, so model moves are deliberate.
  - AGENT_MODEL and SQL_MODEL are separate env vars; the SQL step's model can
    be escalated (Haiku -> Sonnet) independently of the orchestrator. The SQL
    call is made in sql_tool.py with SQL_MODEL; this loop uses AGENT_MODEL.
  - Loop bounds: hard turn cap AND a wall-clock timeout independent of
    Gunicorn's --timeout (must fire before Gunicorn kills the worker).
  - On total failure (API error, timeout, truncation, no finalize), fall back
    to the rule-based parser. query_parser_rules.py is the permanent net.
  - Full tool-call trace captured and returned in the API response.

Quick fixes folded in after the max_tokens-truncation incident:
  - max_tokens is configurable (AGENT_MAX_TOKENS) and defaults high enough
    that a finalize turn can't truncate. With handles, turns are tiny anyway.
  - The system prompt forbids prose between tool calls (all explanation goes
    in finalize_search), so the model doesn't burn a turn narrating.
  - stop_reason == "max_tokens" is logged distinctly as truncation (a config
    problem) rather than being silently lumped in with "model gave up".
"""

import json
import logging
import time
from datetime import datetime, timezone

import anthropic

import config
import tools as tools_mod

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT_TEMPLATE = """You are a photo-search agent for a personal photo library. \
Given a natural-language query, find the matching photos by calling the \
provided tools, then call finalize_search exactly once with the final result \
handle and a short explanation.

Today's date is {today}. Use it to resolve any relative or bare date in the \
query (e.g. "last month", "this summer", or a bare month) to a concrete \
range; do not guess the year.

Result handles:
- search_photos, run_readonly_sql (photo queries), and combine_results each \
return a HANDLE to a stored result set plus a count — never the photo ids \
themselves. You reason about handles ("result_1, 46 photos"); you never see or \
need the ids. Pass a handle to finalize_search to return those photos.

You get exactly ONE query and cannot ask the user anything back — there is no \
conversation, and any question you write is discarded and the search fails. So \
NEVER ask for clarification. Always act on the query as given, making the most \
reasonable interpretation and proceeding. In particular, a bare name (e.g. \
"Kevin") means "photos containing that person" — resolve the name to a person \
id and search; do not stop to ask what kind of photos are wanted. If a query \
is genuinely ambiguous, pick the most likely reading, run the search, and note \
the assumption in finalize_search's explanation.

How to work:
- To filter by a person, resolve their name to a person UUID first: call \
run_readonly_sql for a fuzzy name lookup (it returns the id inline), then pass \
the id to search_photos. If that lookup returns NO rows, do NOT give up — the \
query name may be a nickname, initials, or maiden name that ILIKE can't match \
(e.g. "Rebecca" vs stored "Becky"). Retry ONCE by selecting ALL people \
(SELECT id, name FROM person) and reason over that short list yourself to pick \
the intended person. Only conclude no such person exists after that.
- To filter by place, resolve the user's place name to a real stored city \
value the same way — the library stores specific EXIF-derived places (e.g. \
"Manhattan"), which may differ from a colloquial name (e.g. "New York"). When \
resolving a place or region, ALWAYS select city, state, AND country together \
(SELECT DISTINCT city, state, country FROM asset_exif WHERE city IS NOT NULL) \
— the state and country are what disambiguate a bare city name (e.g. \
"Edgewater" could be NJ, FL, or CO; only the state tells you which). Reason \
over that short list using all three fields, then pass the matching city \
value(s) to search_photos. Do this on the first lookup, not only as a retry.
- For an object or scene description ("beach", "a dog"), pass it as \
object_query to search_photos. object_query is a SINGLE description — for "beach \
OR mountain", do two searches and union them (see combine_results below).
- Filters within search_photos each choose a match mode. people.match "all" \
means the photo contains EVERY listed person (Kevin AND Sarah together); "any" \
means ANY of them (Kevin OR Sarah). cities.match "any" means taken in any of \
the listed cities — use this for a REGION by listing its cities (resolve the \
region to its stored cities via run_readonly_sql first, selecting city, state, \
and country so you can tell which cities actually fall in the region, then pass \
the matching cities). \
Do not intersect separate single-city searches to cover a region — that yields \
nothing; use one cities:any search, or union the per-city handles.
- For predicates search_photos can't express — "only person X in the photo and \
nobody else", text visible in the image, geo proximity — use run_readonly_sql \
to SELECT the photo set (it returns a handle).
- combine_results merges result-set handles with mode 'union' (base OR any \
filter — e.g. beach photos plus mountain photos, or Manhattan photos plus \
Edgewater photos), 'intersect' (base AND all filters — e.g. beach photos that \
are ALSO only-Kevin-in-frame), or 'subtract' (base minus the filters). Do NOT \
merge photo ids yourself — you don't have them; use combine_results. Pick the \
mode deliberately: alternatives/OR -> union; narrowing/AND -> intersect.
- Prefer the fewest tool calls that answer the query correctly. A simple \
object search with no person/place/date is one search_photos call, then \
finalize_search.

A zero-result search means only that nothing matched THIS query — never state \
or imply that the library is empty or unindexed. Just report that no photos \
matched the query.

Do NOT write explanatory prose between tool calls. Put all of your explanation \
into finalize_search's explanation field, and be honest there if a filter \
couldn't be resolved or results are partial."""


def _to_text_block(content):
    """Extract concatenated text from an assistant message's content blocks."""
    return "".join(b.text for b in content if b.type == "text")


def run_search_agent(query_text, immich, claude_client):
    """
    Run the agent loop for a single query.

    Returns a dict:
        {"asset_ids": [...], "explanation": str, "trace": [...], "fell_back": bool}

    Never raises for expected failure modes — on API error, timeout,
    truncation, or a loop that never finalises, it falls back to the rule-based
    parser and marks fell_back=True.
    """
    trace = []
    started = time.monotonic()
    deadline = started + config.AGENT_WALL_CLOCK_TIMEOUT

    store = tools_mod.ResultStore()
    tool_schemas = tools_mod.build_tool_schemas(include_sql=config.AGENT_SQL_ENABLED)

    # Inject today's date so the model resolves relative/bare dates correctly
    # rather than guessing the year (a bare-month query previously landed on the
    # wrong year and returned nothing).
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )

    # Map tool name -> executor. finalize_search is handled specially in the
    # loop (it resolves a handle and terminates), so it's not here.
    executors = {
        "search_photos": lambda **kw: tools_mod.execute_search_photos(immich, store, **kw),
        "combine_results": lambda **kw: tools_mod.execute_combine_results(store, **kw),
    }
    if config.AGENT_SQL_ENABLED:
        import sql_tool
        executors["run_readonly_sql"] = lambda **kw: sql_tool.execute_run_readonly_sql(
            store, claude_client=claude_client, **kw
        )

    messages = [{"role": "user", "content": query_text}]

    try:
        for turn in range(config.AGENT_MAX_TURNS):
            if time.monotonic() > deadline:
                logger.warning("Agent wall-clock timeout — falling back to rules")
                return _fallback(query_text, immich, trace, reason="timeout")

            remaining = max(1.0, deadline - time.monotonic())
            response = claude_client.messages.create(
                model=config.AGENT_MODEL,
                max_tokens=config.AGENT_MAX_TOKENS,
                system=system_prompt,
                tools=tool_schemas,
                messages=messages,
                timeout=remaining,
            )

            if response.stop_reason != "tool_use":
                text = _to_text_block(response.content)
                if response.stop_reason == "max_tokens":
                    # A config problem, not the model giving up. With handles a
                    # finalize turn is tiny, so this should essentially never
                    # fire; log loudly if it does.
                    logger.error(
                        f"Agent turn TRUNCATED at max_tokens "
                        f"({config.AGENT_MAX_TOKENS}) — raise AGENT_MAX_TOKENS. "
                        f"Partial text: {text!r}"
                    )
                    return _fallback(query_text, immich, trace,
                                     reason="max_tokens_truncation")
                logger.warning(
                    f"Agent stopped without tool_use "
                    f"(stop_reason={response.stop_reason}): {text!r}"
                )
                return _fallback(query_text, immich, trace, reason="no_tool_use")

            # Append the assistant turn verbatim (required for the tool-result
            # follow-up to be valid).
            messages.append({"role": "assistant", "content": response.content})

            final_result = None
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "finalize_search":
                    handle = block.input.get("handle")
                    explanation = block.input.get("explanation", "") or ""
                    asset_ids = store.get(handle)
                    if asset_ids is None:
                        # Bad handle — return an error result and let the model
                        # retry rather than terminating with nothing.
                        result_json = json.dumps({
                            "error": f"unknown handle {handle!r}; call a search "
                                     f"tool first and use the handle it returns."
                        })
                        trace.append({
                            "turn": turn, "tool": "finalize_search",
                            "input": block.input, "error": True,
                            "result_preview": result_json[:500],
                        })
                        tool_results.append({
                            "type": "tool_result", "tool_use_id": block.id,
                            "content": result_json, "is_error": True,
                        })
                        continue
                    trace.append({
                        "turn": turn, "tool": "finalize_search",
                        "input": {"handle": handle, "count": len(asset_ids),
                                  "explanation": explanation},
                    })
                    final_result = {
                        "asset_ids": asset_ids, "explanation": explanation,
                        "trace": trace, "fell_back": False,
                    }
                    break  # terminal — stop processing this turn's blocks

                # Ordinary tool call — execute and collect the result.
                try:
                    result = executors[block.name](**block.input)
                    result_json = json.dumps(result)
                    is_error = bool(isinstance(result, dict) and result.get("error"))
                except KeyError:
                    result_json = json.dumps({"error": f"unknown tool {block.name}"})
                    is_error = True
                except Exception as e:
                    logger.warning(f"Tool {block.name} raised: {e}")
                    result_json = json.dumps({"error": str(e)})
                    is_error = True

                trace.append({
                    "turn": turn, "tool": block.name, "input": block.input,
                    "error": is_error, "result_preview": result_json[:500],
                })
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": result_json, "is_error": is_error,
                })

            if final_result is not None:
                elapsed = round(time.monotonic() - started, 2)
                logger.info(
                    f"Agent finalised in {turn + 1} turn(s), {elapsed}s, "
                    f"{len(final_result['asset_ids'])} ids"
                )
                return final_result

            messages.append({"role": "user", "content": tool_results})

        # Turn cap hit without finalize_search.
        logger.warning(f"Agent hit turn cap ({config.AGENT_MAX_TURNS}) — falling back")
        return _fallback(query_text, immich, trace, reason="turn_cap")

    except anthropic.APIError as e:
        logger.warning(f"Anthropic API error ({e}) — falling back to rules")
        return _fallback(query_text, immich, trace, reason="api_error")
    except Exception as e:
        logger.error(f"Unexpected agent error ({e}) — falling back to rules")
        return _fallback(query_text, immich, trace, reason="unexpected")


def _fallback(query_text, immich, trace, reason):
    """
    Permanent safety net: rule-based parser + the same rank-then-filter
    retrieval search_photos uses. Degrades search quality rather than failing.
    """
    import query_parser_rules as rules

    parsed = rules.parse_query(query_text)
    person_ids = [
        pid for pid in (immich.find_person_id(n) for n in parsed.person_names)
        if pid
    ]
    people = {"ids": person_ids, "match": "all"} if person_ids else None
    cities = {"values": [parsed.location], "match": "any"} if parsed.location else None
    asset_ids = tools_mod.run_ranked_search(
        immich,
        object_query=parsed.object_query,
        people=people,
        cities=cities,
        date_from=parsed.date_from,
        date_to=parsed.date_to,
    )
    return {
        "asset_ids": asset_ids,
        "explanation": (
            "Search agent unavailable — used the rule-based fallback parser. "
            "Results may be less precise than usual."
        ),
        "trace": trace + [{"fallback": reason}],
        "fell_back": True,
    }
