# TypeScript & Next.js Style Guide

## Core Principles
1. **Strict Typing**: No `any`. Utilize Discriminated Unions where applicable.
2. **Components**: Use Functional Components with React Hooks.
3. **State**: Prefer local state. If global state is needed, use Zustand over Redux.
4. **Data Fetching**: Use SWR or React Query for client-side fetching. Server Components for initial load.
5. **Styling**: Tailwind CSS strongly preferred. Keep components un-opinionated.

## Conventions
- Interfaces: Prefix with `I` (e.g., `IUser`) or use types directly.
- File Names: `kebab-case` for general files; `PascalCase` for React components.
- Comments: JSDoc for public API interfaces; minimal inline comments (code should be self-documenting).
