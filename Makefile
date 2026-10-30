.PHONY: help install-frontend install-backend run-frontend run-backend test-frontend test-backend test-ultron test-architecture test-all

help:
	@echo "Operation Shadow — Multi-Vehicle C2 Mission Control"
	@echo ""
	@echo "Frontend Commands (React + Vite + TypeScript):"
	@echo "  make install-frontend   Install frontend dependencies"
	@echo "  make run-frontend       Start React Tactical C2 Dashboard on http://localhost:5173"
	@echo "  make test-frontend      Run frontend typecheck and Vitest test suite"
	@echo ""
	@echo "Backend & Agent Commands (FastAPI + ULTRON + SQLite):"
	@echo "  make install-backend    Install backend Python dependencies"
	@echo "  make run-backend        Start FastAPI C2 Server on http://localhost:8000"
	@echo "  make test-backend       Run backend pytest suite"
	@echo "  make test-ultron        Run ULTRON agent unit & pipeline tests"
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
