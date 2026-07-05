NAS_MOUNT_PATH := /mnt/nas

.PHONY: help check-nas setup build up down restart logs status pull clean

help:
	@echo "make up        — verify NAS, start containers"
	@echo "make down      — stop containers"
	@echo "make restart   — down + up"
	@echo "make logs      — tail all container logs"
	@echo "make status    — container status + NAS mount status"
	@echo "make pull      — git pull + rebuild + restart"
	@echo "make clean     — remove containers, images, volumes"
	@echo ""
	@echo "Note: Dropbox is mounted inside the immich-server container itself"
	@echo "(via rclone), not on the host — nothing to mount here for that."

check-nas:
	@mountpoint -q $(NAS_MOUNT_PATH) || (echo "NAS not mounted at $(NAS_MOUNT_PATH) — mount it first" && exit 1)

setup: check-nas
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
	@echo ""
	@mountpoint -q $(NAS_MOUNT_PATH) && echo "NAS: mounted" || echo "NAS: NOT mounted"

pull:
	git pull
	docker compose up -d --build

clean:
	docker compose down --rmi local -v
