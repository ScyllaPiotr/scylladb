# ScyllaDB Development Guide for AI Agents

## Project Context

High-performance distributed NoSQL database (C++23, Seastar framework).
Core values: performance, correctness, readability.

## Build System

### configure.py + ninja

```bash
# Configure (run once per mode, or when switching modes)
./configure.py --mode=<mode>  # mode: dev, debug, release, sanitize

# Build everything for a mode
ninja <mode>-build  # e.g., ninja dev-build

# Build Scylla binary only (sufficient for Python integration tests)
ninja build/<mode>/scylla

# Build a specific test
ninja build/<mode>/test/boost/<test_name>
```

### Frozen toolchain (Docker)

Prefix any command with `./tools/toolchain/dbuild` to use the official build environment:
```bash
./tools/toolchain/dbuild ./configure.py --mode=dev
./tools/toolchain/dbuild ninja dev-build
```

## Running Tests

### C++ Unit Tests

```bash
# Run all tests in a file
./test.py --mode=<mode> test/<suite>/<test_name>.cc

# Run a single test case from a file
./test.py --mode=<mode> test/<suite>/<test_name>.cc::<test_case_name>

# Examples
./test.py --mode=dev test/boost/memtable_test.cc
./test.py --mode=dev test/raft/raft_server_test.cc::test_check_abort_on_client_api
```

**Important:**
- Use full path with `.cc` extension (e.g., `test/boost/test_name.cc`, not `boost/test_name`)
- `test.py` does NOT automatically rebuild when source files change — rebuild with ninja first
- Many tests are part of composite binaries (e.g., `combined_tests`); check `configure.py` or `test/<suite>/CMakeLists.txt` to find which binary contains a test
- Rebuild a composite binary: `ninja build/<mode>/test/<suite>/<binary_name>`
- If you encounter permission issues with cgroup metrics, add `--no-gather-metrics`

**Direct execution of C++ tests:**
```bash
build/dev/test/boost/<test_name> -t <test_case> -- -c1 -m1G
```

### Python Integration Tests

```bash
# Only requires Scylla binary (full build usually not needed)
ninja build/<mode>/scylla

# Run all tests in a file (no .py extension)
./test.py --mode=<mode> <test_path>

# Run a single test case
./test.py --mode=<mode> <test_path>::<test_function_name>

# Examples
./test.py --mode=dev alternator/
./test.py --mode=dev cluster/test_raft_voters::test_raft_limited_voters_retain_coordinator
```

**Important:**
- Use path without `.py` extension (e.g., `cluster/test_raft_no_quorum`)
- Flags: `-v` (verbose), `--repeat N`, `--no-gather-metrics`

## Lint and Formatting

- **clang-format**: config in `.clang-format`; run `clang-format -i <file>` (only format code you modify)
- **Header self-containedness**: `ninja dev-headers` (after adding/removing headers, `touch configure.py` first)
- **License header**: new `.cc`, `.hh`, `.py` files must contain `LicenseRef-ScyllaDB-Source-Available-1.0` in the first 10 lines
- **clang-tidy**: runs in CI on PRs; checks `bugprone-use-after-move`

## C++ Code Style (applies to `*.cc`, `*.hh`)

**Important:** Always match the style and conventions of existing code in the file and directory.

### Naming
- `snake_case` for classes, functions, variables, namespaces, constants/constexpr
- `CamelCase` for template parameters (e.g., `template<typename ValueType>`)
- `_prefix` for private member variables (e.g., `int _count;`)
- No prefix for struct (value-only) members
- Files: `.hh` for headers, `.cc` for source

### Formatting
- 4 spaces indentation, never tabs; 160 character line limit
- K&R braces (opening on same line); brace all scopes, even single statements
- Namespace bodies not indented; closing `} // namespace name`
- `#pragma once` for all headers (no `#ifndef` guards)
- Continuation indent: 8 spaces (double indent)
- Space after keywords (`if (`, `while (`), not after function names
- Minimal patches: only format code you modify, never reformat entire files

### Include Order
1. Own header first (for `.cc` files)
2. C++ standard library (`<vector>`, `<map>`)
3. Seastar headers with angle brackets (`<seastar/core/future.hh>`)
4. Boost headers
5. Project-local headers with quotes (`"db/config.hh"`)

Forward declare when possible. Never `using namespace` in headers.

### Memory Management
- Stack allocation preferred; `std::unique_ptr` by default for dynamic allocations
- `new`/`delete` forbidden — use RAII
- `seastar::lw_shared_ptr` for shared ownership within same shard
- `seastar::foreign_ptr` for cross-shard; avoid `std::shared_ptr`

### Seastar Async Patterns
- `seastar::future<T>` for all async operations
- Prefer coroutines (`co_await`/`co_return`) over `.then()` chains
- `seastar::gate` for shutdown coordination; `seastar::semaphore` for resource limiting
- `maybe_yield()` in long loops to avoid reactor stalls; no blocking calls
- `sstring` (not `std::string`); `logging::logger` per module
- Many files include `seastarx.hh`, which introduces common Seastar names; follow existing file/local conventions for `seastar::` qualification

### Error Handling
- Throw exceptions for errors (futures propagate them automatically)
- In data path: use `std::expected` or `boost::outcome` instead of exceptions
- `SCYLLA_ASSERT` for critical invariants (`utils/assert.hh`)
- `on_internal_error()` for should-never-happen conditions

### Type Safety
- `bool_class<Tag>` instead of raw `bool` parameters
- `enum class` always (never unscoped `enum`)
- Strong typedefs for IDs and domain-specific types

### Forbidden
`malloc`/`free`, `printf`, raw owning pointers, `using namespace` in headers,
blocking ops (`std::sleep`, `std::mutex`), `std::atomic`, new ad-hoc macros (prefer `constexpr`/inline functions; established project macros like `SCYLLA_ASSERT` are fine).

## Python Code Style (applies to `*.py`)

- PEP 8; 160 character line limit; 4 spaces indentation
- Import order: standard library, third-party, local (never `from x import *`)
- Type hints for function signatures (unless directory style omits them — e.g., `test/cqlpy`, `test/alternator`)
- f-strings for formatting
- `@pytest.mark.xfail` for currently-failing tests; unmark when fixed
- Descriptive test names; docstrings explain what the test verifies and why

## Code Philosophy

- Performance matters in hot paths (data read/write, inner loops)
- Self-documenting code through clear naming; comments explain "why", not "what"
- Prefer standard library over custom implementations
- Strive for simplicity; add complexity only when clearly justified
- Question requests: evaluate trade-offs, identify issues, suggest better alternatives

## Test Philosophy

- **Speed**: tests should run as quickly as possible; sleeps are highly discouraged
- **Stability**: run new tests 100x to verify (`--repeat 100 --max-failures 1`)
- Unit tests should test one thing only
- Bug-fix tests must reference the issue and demonstrate the failure before the fix
- Debug mode: reduce iterations/data since tests are always slower
- Repeatable: no random input; consume minimal resources (prefer single-node if sufficient)

## Commit Messages

Format: `module: short description` (e.g., `cql3: fix prepared statement cache eviction`)
Multiple modules: `cql3, transport: fix ...` — whole tree: `tree: ...`
Body must include motivation (the "why", not just the "what").
Maintain bisectability: all tests must pass in every commit.

## See Also

- `.github/copilot-instructions.md` — primary AI assistant instructions
- `.github/instructions/cpp.instructions.md` — detailed C++ rules
- `.github/instructions/python.instructions.md` — detailed Python rules
- `CONTRIBUTING.md`, `HACKING.md`, `docs/dev/review-checklist.md`
