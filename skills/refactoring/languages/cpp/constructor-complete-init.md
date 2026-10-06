# C++ Refactoring: Constructor Complete Initialization

## Principle

After the constructor returns, the object must be fully initialized and immediately usable. No method call should be required before the object can be used safely.

## Why `Initialize()` Methods Are an Anti-Pattern

A two-phase construction pattern — constructor + `Initialize()` / `Init()` / `Open()` / `Setup()` — creates a zombie state: an object that exists but cannot be used. This forces every caller to remember to call `Initialize()` and leaves the class in an indeterminate state between construction and initialization.

Problems this causes:

- **Invariant violation**: Class invariants cannot be established in the constructor, so they cannot be relied upon anywhere else. Every method must defensively check whether initialization happened.
- **Undefined behavior risk**: Using the object before `Initialize()` is called is either silently wrong or crashes. The type system does not prevent it.
- **Error handling obscured**: Constructors cannot return error codes, so `Initialize()` is often introduced to handle failures that should have been constructor failures. The correct solution is exceptions or a factory function — not deferred initialization.
- **Increased cognitive load**: Callers must know about a two-step protocol that the type signature does not express.
- **Thread safety complexity**: A partially initialized object shared across threads requires extra synchronization that a fully initialized object would not need.

## How to Refactor Away `Initialize()` Methods

### Option 1: Move initialization into the constructor

If initialization can fail, throw an exception. This is the idiomatic C++ approach.

Before:
```cpp
class Connection {
public:
    Connection() {}
    bool Initialize(const std::string& host, int port);  // anti-pattern
private:
    Socket socket_;
    bool initialized_ = false;
};

// Caller
Connection conn;
if (!conn.Initialize("localhost", 8080)) { /* handle error */ }
conn.send(data);  // what if Initialize was forgotten?
```

After:
```cpp
class Connection {
public:
    Connection(const std::string& host, int port);  // throws on failure
private:
    Socket socket_;
};

// Caller
Connection conn("localhost", 8080);  // fully usable immediately, or exception thrown
conn.send(data);
```

### Option 2: Use a factory function when construction can fail and exceptions are unavailable

If the codebase prohibits exceptions, use a static factory that returns `std::optional<T>` or a result type instead of a two-phase init.

```cpp
class Connection {
public:
    static std::optional<Connection> create(const std::string& host, int port);
private:
    explicit Connection(Socket s);  // private: only factory constructs
    Socket socket_;
};

// Caller
auto conn = Connection::create("localhost", 8080);
if (!conn) { /* handle error */ }
conn->send(data);
```

### Option 3: Lazy initialization via `std::optional` member (for genuinely optional sub-resources)

If a sub-resource is conditionally needed and acquiring it is expensive, hold it in a `std::optional` or `unique_ptr` member — but the object itself must still be fully usable after construction. The optional member is an implementation detail, not a public initialization step.

## Refactoring Checklist

Before removing an `Initialize()` method:

- [ ] Identify every resource or state the method sets up.
- [ ] Move that setup into the constructor. Use member initializer lists where possible.
- [ ] If setup can fail: choose exceptions (preferred) or a factory function.
- [ ] Remove the `initialized_` / `ready_` flag and all defensive checks that gate on it.
- [ ] Delete the `Initialize()` method and update all call sites.
- [ ] Verify that class invariants now hold unconditionally after construction.
- [ ] Run the full verification gate before moving on.

## Stop Conditions

Do not apply this refactoring when:

- The codebase has a project-wide policy against exceptions and no result/optional type is available — resolve the error-handling policy first.
- The `Initialize()` method is part of a virtual interface or plugin protocol that cannot be changed without breaking ABI or external consumers.
- Construction requires async operations that cannot complete synchronously — in that case, the async completion callback or coroutine resumption is the true constructor boundary; model it as such rather than adding a sync `Initialize()`.
