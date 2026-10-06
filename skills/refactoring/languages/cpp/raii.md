# C++ Refactoring: RAII

## What RAII Is

**Resource Acquisition Is Initialization** (RAII) is a C++ idiom where a resource's lifetime is bound to the lifetime of an object. The resource is acquired in the constructor and released unconditionally in the destructor. Because C++ guarantees destructor calls when objects go out of scope — including during stack unwinding on exceptions — RAII makes resource management automatic and exception-safe without explicit cleanup code.

Resources in this context include anything requiring paired acquire/release operations: heap memory, file handles, mutex locks, socket connections, GPU buffers, database transactions, and any OS or hardware resource.

## How It Works

```cpp
// Without RAII — manual, error-prone
void process() {
    FILE* f = fopen("data.bin", "rb");
    if (!f) return;
    // ... if an exception fires here, f is never closed
    fclose(f);
}

// With RAII — automatic, exception-safe
void process() {
    std::ifstream f("data.bin", std::ios::binary);
    if (!f) return;
    // ... destructor closes f when process() returns or throws
}
```

The RAII wrapper owns the resource. Its destructor is the single, unconditional release point. Callers never call `close`, `free`, `unlock`, or `release` manually.

## The Standard RAII Toolkit

The standard library provides RAII wrappers for the most common resources. Prefer these over raw handles:

| Resource | Raw handle | RAII wrapper |
|---|---|---|
| Heap memory (single object) | `T*` + `delete` | `std::unique_ptr<T>` |
| Heap memory (shared ownership) | `T*` + ref-counted `delete` | `std::shared_ptr<T>` |
| Heap memory (array) | `T*` + `delete[]` | `std::unique_ptr<T[]>` or `std::vector<T>` |
| Mutex lock | `mtx.lock()` / `mtx.unlock()` | `std::lock_guard` / `std::unique_lock` |
| File | `FILE*` + `fclose` | `std::ifstream` / `std::ofstream` |

For resources not covered by the standard library, write a small wrapper class (see below).

## How RAII Helps Refactoring

### 1. Replace manual cleanup with RAII wrappers (most common)

**Smell:** `goto cleanup`, paired `open`/`close` calls, `try`/`catch` blocks whose sole purpose is to release a resource, or early-return paths that duplicate cleanup.

**Refactoring:** Wrap the raw handle in an RAII type. Remove every manual release call. The scope boundary becomes the release point.

Before:
```cpp
void render(DeviceContext* ctx) {
    Texture* tex = ctx->createTexture(desc);
    if (!tex) return;
    Buffer* buf = ctx->createBuffer(size);
    if (!buf) {
        ctx->destroyTexture(tex);   // easy to forget
        return;
    }
    // ... work ...
    ctx->destroyBuffer(buf);
    ctx->destroyTexture(tex);
}
```

After:
```cpp
void render(DeviceContext* ctx) {
    auto tex = TextureHandle(ctx, desc);   // RAII wrapper
    if (!tex) return;
    auto buf = BufferHandle(ctx, size);    // RAII wrapper
    if (!buf) return;
    // ... work ...
}   // destructors release in reverse order automatically
```

### 2. Replace raw owning pointers with smart pointers

**Smell:** `new` without a corresponding `delete` in the same scope, or `delete` in multiple branches.

**Refactoring:** Replace `T* p = new T(...)` with `auto p = std::make_unique<T>(...)`. Remove all `delete p` calls. Transfer or share ownership explicitly using `std::move` or `std::shared_ptr` when needed.

### 3. Replace manual lock/unlock with lock guards

**Smell:** `mtx.lock()` at the top of a function with `mtx.unlock()` at the bottom (or in every return path).

**Refactoring:** Replace with `std::lock_guard<std::mutex> lk(mtx);` at the point of acquisition. Remove all `unlock()` calls.

### 4. Introduce a custom RAII wrapper for non-standard resources

When no standard wrapper exists (GPU handle, COM object, C library handle), write a minimal wrapper:

```cpp
class GpuBuffer {
public:
    explicit GpuBuffer(GpuDevice& dev, size_t size)
        : dev_(dev), handle_(dev.allocBuffer(size)) {}
    ~GpuBuffer() { if (handle_) dev_.freeBuffer(handle_); }

    GpuBuffer(const GpuBuffer&) = delete;
    GpuBuffer& operator=(const GpuBuffer&) = delete;
    GpuBuffer(GpuBuffer&& o) noexcept : dev_(o.dev_), handle_(std::exchange(o.handle_, nullptr)) {}
    GpuBuffer& operator=(GpuBuffer&&) noexcept = delete;

    GpuHandle get() const { return handle_; }
    explicit operator bool() const { return handle_ != nullptr; }

private:
    GpuDevice& dev_;
    GpuHandle handle_;
};
```

The Rule of Five (or Zero) applies: if you define a destructor, define or delete copy and move operations explicitly.

## Refactoring Checklist for RAII

Before applying an RAII refactoring, confirm:

- [ ] The resource has exactly one owner at each point in time (unique ownership → `unique_ptr` or custom wrapper; shared ownership → `shared_ptr`).
- [ ] The destructor release is unconditional and idempotent (safe to call even if acquisition partially failed).
- [ ] Copy is deleted or correctly implemented (copying a resource handle usually means copying the underlying resource, which is rarely the intent).
- [ ] Move is implemented if the wrapper needs to be transferred (e.g., returned from a factory function).
- [ ] Every manual release call (`delete`, `free`, `close`, `unlock`, etc.) has been removed after wrapping.
- [ ] The wrapped code is covered by tests before refactoring (characterization tests if needed).

## Stop Conditions

Do not apply an RAII refactoring when:

- The resource has shared or ambiguous lifetime that cannot be modeled by a single owning wrapper without significant restructuring — resolve ownership design first.
- The code is in a `noexcept` destructor chain and the release operation can throw — handle the exception inside the destructor explicitly.
- The resource is managed by a third-party framework that takes ownership — do not double-wrap it.
