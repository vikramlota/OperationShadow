.PHONY: help install-frontend install-backend run-frontend run-backend test-frontend test-backend test-ultron test-architecture test-all scout01-ssh scout01-status scout01-stop scout01-tunnel

help:
	@echo "Operation Shadow — Robotics Mission Control & Operations Console"
	@echo ""
	@echo "Frontend Commands (React + Vite + TypeScript):"
	@echo "  make install-frontend   Install frontend dependencies"
	@echo "  make run-frontend       Start React Operations Dashboard on http://localhost:5173"
	@echo "  make test-frontend      Run frontend typecheck and Vitest test suite"
	@echo ""
	@echo "Backend & Agent Commands (FastAPI + ULTRON + SQLite):"
	@echo "  make install-backend    Install backend Python dependencies"
	@echo "  make run-backend        Start FastAPI Mission-Control Server on http://localhost:8000"
	@echo "  make test-backend       Run backend pytest suite"
	@echo "  make test-ultron        Run ULTRON agent unit & pipeline tests"
	@echo ""
	@echo "SCOUT01 Remote ROS 2 Stack (OAK-D + Unitree L2 LiDAR via SSH):"
	@echo "  make scout01-ssh        SSH to chawlaji@10.68.44.41, run ~/scout01_start.py start, and forward :9090 & :8081"
	@echo "  make scout01-status     Check ~/scout01_start.py status over SSH"
	@echo "  make scout01-stop       Stop managed SCOUT01 nodes via ~/scout01_start.py stop"
	@echo "  make scout01-tunnel     Forward local :9090 (ROSBridge) and :8081 (OAK MJPEG) over SSH"
	@echo ""
	@echo "System Verification:"
	@echo "  make test-architecture  Verify repository structure & segregation"
	@echo "  make test-all           Run all verification tests across all tiers"

install-frontend:
	cd frontend && npm install

run-frontend:
	cd frontend && npm run dev

test-frontend:
	cd frontend && npm run typecheck && npm test -- --run

install-backend:
	cd backend && pip install -r requirements.txt

run-backend:
	cd backend && PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000

test-backend:
	cd backend && PYTHONPATH=fastapi:.:.. pytest

test-ultron:
	PYTHONPATH=.:backend:backend/fastapi pytest tests

test-architecture:
	python3 -c "from tests.test_system_architecture import test_trd_section_26_repository_layout, test_segregated_frontend_backend_hardware_layout; test_trd_section_26_repository_layout(); test_segregated_frontend_backend_hardware_layout(); print('Architecture & segregation layout verified!')"

test-all: test-architecture test-ultron test-frontend test-backend

scout01-ssh:
	bash scripts/scout01_ssh.sh start

scout01-status:
	bash scripts/scout01_ssh.sh status

scout01-stop:
	bash scripts/scout01_ssh.sh stop

scout01-tunnel:
	bash scripts/scout01_ssh.sh tunnel
