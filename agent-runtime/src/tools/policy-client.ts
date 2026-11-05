/**
 * Python 权限桥接客户端：policy/check 与 policy/confirm 的最小 HTTP 封装。
 *
 * 契约（与 src/guanjia/bridge_service.py 对齐）：
 *   POST /internal/policy/check   {session_id, capability_id, args, intent?, task_id?}
 *                                 → {outcome, reason, todo_id?, summary?}
 *   POST /internal/policy/confirm {todo_id, decision}
 *                                 → {credential} | {denied: true}
 * 鉴权：Authorization: Bearer <token>。
 */

export type PolicyOutcome = 'allow' | 'deny' | 'confirmation_required';

export interface PolicyCheckRequest {
  session_id: string;
  capability_id: string;
  args: Record<string, unknown>;
  intent?: string;
  task_id?: string;
  /** 审批后重试时的一次性凭证（deps.credential 透传；bridge 校验通过按 allow 放行）。 */
  credential?: string;
}

export interface PolicyCheckResponse {
  outcome: PolicyOutcome;
  reason: string;
  todo_id?: string;
  /** confirmation_required 时给口头/展示用的摘要（PolicyConfirmationError.summary）。 */
  summary?: string;
}

export interface PolicyConfirmResponse {
  credential?: string;
  denied?: boolean;
}

async function postJson(
  url: string,
  token: string,
  body: Record<string, unknown>,
): Promise<{ status: number; json: Record<string, unknown> }> {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
  let json: Record<string, unknown> = {};
  try {
    json = (await response.json()) as Record<string, unknown>;
  } catch {
    // 非 JSON 响应按状态码处理
  }
  return { status: response.status, json };
}

/** bridge 的 Policy 客户端；一个实例承载同一个 token 的全部调用。 */
export class PolicyClient {
  constructor(
    private readonly bridgeUrl: string,
    private readonly token: string,
  ) {}

  /** 权限检查；网络/服务异常按 deny 处理（fail-closed，不给静默放行留缝）。 */
  async check(req: PolicyCheckRequest): Promise<PolicyCheckResponse> {
    let outcome: PolicyCheckResponse;
    try {
      const { status, json } = await postJson(
        `${this.bridgeUrl}/internal/policy/check`,
        this.token,
        req as unknown as Record<string, unknown>,
      );
      if (status !== 200) {
        return { outcome: 'deny', reason: `bridge policy check http_${status}` };
      }
      outcome = {
        outcome: (json.outcome as PolicyOutcome) ?? 'deny',
        reason: String(json.reason ?? 'bridge returned no reason'),
        todo_id: typeof json.todo_id === 'string' ? json.todo_id : undefined,
        summary: typeof json.summary === 'string' ? json.summary : undefined,
      };
    } catch (err) {
      return { outcome: 'deny', reason: `bridge unreachable: ${err instanceof Error ? err.message : String(err)}` };
    }
    if (outcome.outcome !== 'allow' && outcome.outcome !== 'deny' && outcome.outcome !== 'confirmation_required') {
      return { outcome: 'deny', reason: 'bridge returned unknown outcome' };
    }
    return outcome;
  }

  /** 审批决定：approve 返回一次性 credential；reject 返回 {denied: true}。 */
  async confirm(todoId: string, decision: 'approve' | 'reject'): Promise<PolicyConfirmResponse> {
    const { status, json } = await postJson(`${this.bridgeUrl}/internal/policy/confirm`, this.token, {
      todo_id: todoId,
      decision,
    });
    if (status !== 200) {
      throw new Error(`bridge policy confirm http_${status}: ${JSON.stringify(json)}`);
    }
    if (json.denied === true) return { denied: true };
    if (typeof json.credential === 'string' && json.credential) return { credential: json.credential };
    throw new Error('bridge policy confirm returned neither credential nor denied');
  }
}
