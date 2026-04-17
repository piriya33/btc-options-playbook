# Python & FastAPI Style Guide

## Core Principles
1. **Typing**: Strict type hints required (`mypy --strict` standard). Use Pydantic models for all data validation.
2. **Async**: Use `async def` for any I/O bound operations (API calls, DB queries).
3. **Structure**: 
   - `models.py` (SQLAlchemy models)
   - `schemas.py` (Pydantic models)
   - `routers/` (FastAPI route definitions)
   - `services/` (Business logic, options math)

## Conventions
- Naming: `snake_case` for functions/variables, `PascalCase` for classes.
- Linting: Use `ruff` for linting and formatting. 
- Docstrings: Use Google-style docstrings for any function executing financial math logic.
- Error Handling: Do not return 500s. Explicitly catch exceptions and return `HTTPException` with clear 4xx status codes.
