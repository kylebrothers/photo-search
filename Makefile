.PHONY: help check-nas-reachable setup build up down restart logs status pull clean

help:
	@echo "make up        — start containers (NAS mounted automatically via Docker NFS volume)"
	@echo "make down      — stop containers"
	@echo "make restart   — down + up"
	@echo "make logs      — tail all container logs"
	@echo "make status    — container status"
	@echo "make pull      — git pull + rebuild + restart"
	@echo "make clean     — remove containers, images, volumes"
	@echo ""
	@echo "Note: Dropbox is mounted inside the immich-server container itself"
	@echo "(via rclone), and the NAS thumbnail path is a Docker-native NFS"
	@echo "volume — neither needs any host-level mount."

check-nas-reachable:
	@set -a; . ./.env 2>/dev/null; set +a; \
	if [ -z "$$NAS_IP" ]; then echo "NAS_IP not set in .env"; exit 1; fi; \
	ping -c1 -W2 $$NAS_IP > /dev/null 2>&1 || \
		(echo "NAS at $$NAS_IP not reachable — check network before continuing" && exit 1)

setup: check-nas-reachable
	@test -f .env || (echo "Missing .env — copy from .env.example" && exit 1)
	@test -f immich-server/rclone.conf || \
		(echo "Missing immich-server/rclone.conf — copy from rclone.conf.example and fill in your Dropbox token" && exit 1)

build: setup
	docker compose build

up: setup
	docker compose up -d
	@echo "photo-search up — Immich: http://localhost:2283  search-api: http://localhost:5000"

down:
	docker compose down

restart: down up

logs:
	docker compose logs -f

status:
	docker compose ps

pull:
	git pull
	docker compose up -d --build

clean:
	docker compose down --rmi local -v
