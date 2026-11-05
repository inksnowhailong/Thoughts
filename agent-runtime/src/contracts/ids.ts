/**
 * 共享标识符与 JSON 基元,镜像 Python 侧契约:
 * - `src/guanjia/core/contracts.py::_identifier`(1..200 字符、无控制字符)
 * - `src/guanjia/contracts/ids.py::validate_identifier`(strip 后非空即可)
 * - `src/guanjia/contracts/events.py::_parse_utc / _format_utc`(ISO-8601 UTC "Z" 时间戳)
 *
 * 注意:Python `len()` 按 Unicode 码点计数,而 JS `string.length` 按 UTF-16 码元
 * (emoji 等增补平面字符会算 2),所以长度校验必须用展开后的码点数对齐。
 */
import { z } from "zod";

/** 按 Unicode 码点计数,对齐 Python `len()`。 */
export function codePointLength(value: string): number {
  return [...value].length;
}

/** 镜像 `core/contracts.py::_identifier`:1..200 码点、无控制字符(U+0000..U+001F、U+007F)。 */
export const identifierSchema = z
  .string()
  .refine(
    (value) => {
      const length = codePointLength(value);
      if (length < 1 || length > 200) return false;
      for (const character of value) {
        const code = character.codePointAt(0) ?? 0;
        if (code < 32 || code === 127) return false;
      }
      return true;
    },
    { message: "must be 1..200 characters without control characters" },
  );

/** 镜像 `contracts/ids.py::validate_identifier`:仅要求 strip 后非空(EventEnvelope 使用)。 */
export const nonEmptyIdentifierSchema = z
  .string()
  .refine((value) => value.trim().length > 0, { message: "must be a non-empty string" });

export type JsonScalar = string | number | boolean | null;
export type JsonValue = JsonScalar | JsonValue[] | { [key: string]: JsonValue };

/**
 * 递归 JSON 值。镜像 Python `_freeze_json`:float 必须有限(NaN/Infinity 拒绝)、
 * 对象键必须是字符串,容器只允许 array/object。
 */
export const jsonValueSchema: z.ZodType<JsonValue> = z.lazy(() =>
  z.union([
    z.string(),
    z.number().refine((value) => Number.isFinite(value), { message: "payload floats must be finite" }),
    z.boolean(),
    z.null(),
    z.array(jsonValueSchema),
    z.record(z.string(), jsonValueSchema),
  ]),
);

/**
 * 镜像 `contracts/events.py::_parse_utc`:必须是 ISO-8601 UTC 字符串且以 "Z" 结尾。
 * 小数秒允许 1..6 位(对齐 Python `datetime.fromisoformat` 的微秒语义)。
 */
const ISO_UTC_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/;

export const isoUtcSchema = z
  .string()
  .refine((value) => ISO_UTC_PATTERN.test(value) && !Number.isNaN(Date.parse(value)), {
    message: "must be an ISO-8601 UTC Z timestamp",
  });

export function parseUtc(value: string): Date {
  return new Date(value);
}

/**
 * 镜像 `contracts/events.py::_format_utc`(`isoformat().replace("+00:00", "Z")`):
 * 微秒为 0 时省略小数部分;非 0 时输出 6 位微秒。JS Date 只有毫秒精度,
 * 故毫秒展开为 ".mmm000Z" 形式。
 */
export function formatUtc(value: Date): string {
  const seconds = value.toISOString().slice(0, 19);
  const millis = value.getUTCMilliseconds();
  return millis === 0 ? `${seconds}Z` : `${seconds}.${millis.toString().padStart(3, "0")}000Z`;
}
