/**
 * 双端共享契约汇总出口。
 *
 * Python 侧不可变 dataclass 是契约的唯一事实来源;本目录的 Zod schema
 * 逐字段镜像其线协议,并通过仓库根 `contracts/fixtures/` 的同一组
 * JSON 样例做双端 round-trip 验证(见 test/contracts/fixtures.roundtrip.test.ts
 * 与 tests/test_agent_runtime_contracts.py)。
 */
export * from "./ids.js";
export * from "./event-envelope.js";
export * from "./task-spec.js";
export * from "./worker-event.js";
export * from "./memory-packet.js";
export * from "./policy.js";
export * from "./task-result.js";
export * from "./tool-result.js";
export * from "./worker-api.js";
