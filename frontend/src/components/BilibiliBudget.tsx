import type { BiliBudgetStop, BiliRun } from "../api/bilibili";
import { formatTime } from "../utils";

const RESOURCES = [
  ["platform", "B 站接口请求"],
  ["llm", "AI 请求"],
  ["videos", "视频详情尝试"],
  ["attempts", "字幕读取尝试"],
  ["verified", "进入字幕核验"],
  ["visual_units", "进入截图复核"],
  ["frames", "截图尝试"],
  ["browser_requests", "播放器请求"],
  ["browser_bytes", "播放器流量"],
  ["seconds", "累计运行时间"],
  ["tokens", "文本额度（估算）"],
] as const;
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
const known = (value: number | null | undefined): value is number => typeof value === "number" && Number.isFinite(value);

function amount(resource: string, value: number | null | undefined) {
  if (!known(value)) return "-";
  if (resource === "browser_bytes") return `${(value / 1048576).toFixed(1)} MiB`;
  return resource === "seconds" ? formatTime(Math.ceil(value) * 1000) : number.format(value);
}

export function budgetLimitReason(stop?: BiliBudgetStop) {
  if (!stop?.resource || !known(stop.used) || !known(stop.limit)) {
    return "已触发运行额度限制，但此记录未保存具体触发项";
  }
  const label = RESOURCES.find(([key]) => key === stop.resource)?.[1] ?? "其他额度";
  const scope = stop.scope === "exact" ? "精准阶段上限" : "本次任务上限";
  const next = known(stop.requested) && stop.requested > 0
    ? `；下一步需 ${amount(stop.resource, stop.requested)}${stop.reserve ? `，含综合核验预留 ${amount(stop.resource, stop.reserve)}` : ""}` : "";
  return `${label}${stop.used >= stop.limit ? "已达到" : "下一步预计超出"}${scope}（已用 ${amount(stop.resource, stop.used)} / 上限 ${amount(stop.resource, stop.limit)}${next}）`;
}

export function BilibiliBudget({ run }: { run: BiliRun }) {
  const stopped = run.stop_reason === "budget" || run.status === "budget_exhausted" || Boolean(run.budget_stop);
  if (!stopped && (!run.budget || !run.limits)) return null;
  return <section className="bili-budget" aria-label="检索预算">
    {stopped && <p className="bili-warning" role="status">
      已暂停：{budgetLimitReason(run.budget_stop)}。已保存完成的核验。
    </p>}
    {run.budget && run.limits && <>
      <h4>检索用量</h4>
      <table aria-label="任务预算用量"><thead><tr>
        <th scope="col">项目</th><th scope="col">已用</th><th scope="col">总上限</th><th scope="col">剩余</th>
      </tr></thead><tbody>
        {RESOURCES.filter(([key]) => known(run.budget?.[key]) || known(run.limits?.[key])).map(([key, label]) => <tr key={key} data-budget-resource={key}
          className={stopped && run.budget_stop?.resource === key ? "bili-budget-hit" : undefined}>
          <th scope="row">{label}</th><td>{amount(key, run.budget?.[key])}</td><td>{amount(key, run.limits?.[key])}</td>
          <td>{amount(key, known(run.budget?.[key]) && known(run.limits?.[key]) ? Math.max(0, run.limits![key] - run.budget![key]) : undefined)}</td>
        </tr>)}
      </tbody></table>
      <p className="bili-muted">文本额度为内部估算，不代表实际 Token 或费用。</p>
    </>}
    {run.phase_limits?.exact && <details className="bili-phase-budget">
      <summary>精准阶段上限</summary>
      <dl>{RESOURCES.filter(([key]) => known(run.phase_limits?.exact?.[key])).map(([key, label]) =>
        <div key={key}><dt>{label}</dt><dd>{amount(key, run.phase_limits?.exact?.[key])}</dd></div>)}</dl>
      <p className="bili-muted">精准阶段触及上限后转入课程检索；用量仍计入整个任务。</p>
    </details>}
  </section>;
}
