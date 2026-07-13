"""
search_agent.py — Tool-calling search agent (Claude API).

Replaces the one-shot "parse into fixed JSON" approach (query_parser_llm.py)
with a tool-calling loop. The agent resolves names/places/dates and composes
retrieval by calling tools, then signals completion with finalize_search.

Design decisions (see README "Agreed design going forward"):
  - Model is pinned via env (AGENT_MODEL), NOT auto-"latest" — SQL/tool
    correctness is prompt-sensitive, so model moves are deliberate, followed
    by re-running the structured test list.
  - AGENT_MODEL and SQL_MODEL are separate env vars. Today they default to
    the same model; the split exists so the SQL tool's model can be escalated
    independently (Haiku -> Sonnet) without moving the orchestrator. The
    per-call swap is realised in sql_tool.py, which makes its own client call
    with SQL_MODEL when run_readonly_sql needs a model; the orchestration loop
    here always uses AGENT_MODEL.
  - Loop bounds: hard turn cap AND a wall-clock timeout independent of
    Gunicorn's --timeout (same race-condition lesson as the Ollama bug —
    this must fire before Gunicorn kills the worker).
  - On total failure (API error, timeout, no finalize), fall back to the
    rule-based parser. query_parser_rules.py remains the permanent safety net.
  - The full tool-call trace is captured and returned in the API response,
    replacing the deterministic-Python-trace debuggability the old design had.
"""

import json
import logging
import time

import anthropic

import config
import tools as tools_mod

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are a photo-search agent for a personal photo library. \
Given a natural-language query, find the matching photos by calling the \
provided tools, then call finalize_search exactly once with the final asset \
ids and a short explanation.

How to work:
- To filter by a person, you must resolve their name to a person UUID first. \
Names in the library may differ from what the user typed (nicknames, partial \
names, misspellings). Use run_readonly_sql to look up the person table by a \
fuzzy name match, then pass the id to search_photos.
- To filter by place, resolve the user's place name to a real stored city \
value the same way — the library stores specific EXIF-derived places (e.g. \
"Manhattan"), which may differ from a colloquial name (e.g. "New York"). \
Use run_readonly_sql against the exif/geo tables to find the closest real \
value, then pass it to search_photos.
- For an object or scene description ("beach", "a dog"), pass it as \
object_query to search_photos — that runs semantic image search.
- For predicates search_photos cannot express — "only person X in the photo \
and nobody else", text visible in the image, or place matching more flexible \
than one exact city — write a single read-only SELECT with run_readonly_sql \
and pass the resulting asset ids straight to finalize_search.
- Prefer the fewest tool calls that answer the query correctly. If the query \
is a simple object search with no person/place/date, a single search_photos \
call is enough.

Be honest in the explanation: if you could not resolve a filter, or results \
are partial, say so plainly."""


def _to_text_block(content):
    """Extract concatenated text from an assistant message's content blocks."""
    return "".join(b.text for b in content if b.type == "text")


def run_search_agent(query_text, immich, claude_client):
    """
    Run the agent loop for a single query.

    Returns a dict:
        {
          "asset_ids":   [str, ...],   # final ordered ids
          "explanation": str,          # user-facing account
          "trace":       [ ... ],      # tool-call trace for debugging
          "fell_back":   bool,         # True if rules fallback was used
        }

    Never raises for expected failure modes — on API error, timeout, or a
    loop that never finalises, it falls back to the rule-based parser and
    marks fell_back=True.
    """
    trace = []
    started = time.monotonic()
    deadline = started + config.AGENT_WALL_CLOCK_TIMEOUT

    tool_schemas = tools_mod.build_tool_schemas(
        include_sql=config.AGENT_SQL_ENABLED
    )

    # Map tool name -> executor. finalize_search and run_readonly_sql are
    # handled specially in the loop, so they're not in this dispatch table.
    executors = {
        "search_photos": lambda **kw: tools_mod.execute_search_photos(immich, **kw),
    }
    if config.AGENT_SQL_ENABLED:
        import sql_tool
        executors["run_readonly_sql"] = lambda **kw: sql_tool.execute_run_readonly_sql(
            claude_client=claude_client, **kw
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
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=tool_schemas,
                messages=messages,
                timeout=remaining,
            )

            if response.stop_reason != "tool_use":
                # Model responded without calling a tool (e.g. asked a
                # question or gave up). Treat as non-finalised -> fall back.
                text = _to_text_block(response.content)
                logger.warning(
                    f"Agent stopped without tool_use (stop_reason="
                    f"{response.stop_reason}): {text!r}"
                )
                return _fallback(query_text, immich, trace, reason="no_tool_use")

            # Append the assistant turn verbatim (required for the tool-result
            # follow-up to be valid).
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "finalize_search":
                    asset_ids = block.input.get("asset_ids", []) or []
                    explanation = block.input.get("explanation", "") or ""
                    trace.append({
                        "turn": turn, "tool": "finalize_search",
                        "input": {"asset_ids_count": len(asset_ids),
                                  "explanation": explanation},
                    })
                    elapsed = round(time.monotonic() - started, 2)
                    logger.info(
                        f"Agent finalised in {turn + 1} turn(s), {elapsed}s, "
                        f"{len(asset_ids)} ids"
                    )
                    return {
                        "asset_ids": asset_ids,
                        "explanation": explanation,
                        "trace": trace,
                        "fell_back": False,
                    }

                # Ordinary tool call — execute and collect the result.
                try:
                    result = executors[block.name](**block.input)
                    result_json = json.dumps(result)
                    is_error = False
                except KeyError:
                    result_json = json.dumps(
                        {"error": f"unknown tool {block.name}"}
                    )
                    is_error = True
                except Exception as e:
                    logger.warning(f"Tool {block.name} raised: {e}")
                    result_json = json.dumps({"error": str(e)})
                    is_error = True

                trace.append({
                    "turn": turn, "tool": block.name,
                    "input": block.input,
                    "error": is_error,
                    "result_preview": result_json[:500],
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_json,
                    "is_error": is_error,
                })

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
    asset_ids = tools_mod.execute_search_photos(
        immich,
        object_query=parsed.object_query,
        person_ids=person_ids,
        city=parsed.location,
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
