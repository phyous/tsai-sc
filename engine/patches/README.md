# Pinned runtime patches

`scripts/setup-runtime.sh` applies these patches to BottleShip commit
`a7c8543d75569d48890d48744897a0ffe3fb02f7`. Reapplying an exact existing patch is
safe; a source mismatch fails setup. The patched runtime stays in `.runtime/`.

`0001-ddraw-present-conversion-buffer-lifetime.patch` fixes a GPU buffer lifetime
gap in pure DirectDraw frames. Palette8 presentation submits its own conversion
encoder, allocating source, parameters, RGBA output and palette buffers. These
enter a shared pending-destruction list. Previously, the executor drained that
list only when it also had an encoder to submit; a pure 2D frame can have none.
At 640×480, one such conversion retains 1,537,080 bytes if cleanup is skipped.
CPU screenshot capture alone does not stop this rendering allocation path.

The patch drains the list at `endFrameForPresent`, immediately after `flush`.
At that point the presenter's encoder has been submitted, and `flush` has
submitted any executor-owned encoder. Calling cleanup directly after the
presenter's earlier submit would risk destroying buffers still referenced by
the executor's unsubmitted encoder. WebGPU permits destruction after submission:
queued work retains the allocation until its uses finish, while subsequent
submissions cannot use the destroyed buffer. No GPU completion wait is needed.

Three Bun tests exercise the actual executor and converter methods with mocked
GPU resources: 100 pure 2D frames leave no pending buffers, mixed frames submit
before destruction, and a failed executor submit leaves its buffers intact.
Setup runs these tests. They establish resource-lifetime behavior, not a browser
memory soak result or proof that this was the only source of memory pressure.
