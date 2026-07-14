.PHONY: all build build-backend build-frontend clean

VENV := .venv

all: build

# Build both backend (venv + editable install) and frontend (bundled assets).
build: build-backend build-frontend

build-backend:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e ./smart_vent

build-frontend:
	cd smart_vent/frontend && npm install && npm run build

clean:
	rm -rf $(VENV) smart_vent/frontend/dist smart_vent/frontend/node_modules
