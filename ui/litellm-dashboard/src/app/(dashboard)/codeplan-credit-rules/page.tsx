"use client";

import useAuthorized from "@/app/(dashboard)/hooks/useAuthorized";
import {
  activateCodePlanCreditRule,
  archiveCodePlanCreditRule,
  createCodePlanCreditRule,
  formatCodePlanError,
  getCodePlanCreditRule,
  listCodePlanAvailableModels,
  listCodePlanCreditRules,
  listCodePlanPlans,
  updateCodePlanCreditRule,
} from "@/components/codeplan/codeplan_networking";
import type {
  CodePlanCreditRule,
  CodePlanCreditRuleCreateRequest,
  CodePlanCreditRulePatchRequest,
  CodePlanPlan,
  CreditRuleStatus,
} from "@/components/codeplan/types";
import { isProxyAdminRole } from "@/utils/roles";
import { Alert, Button, Card, Empty, Tag, Typography, message } from "antd";
import { useCallback, useEffect, useMemo, useState } from "react";

const { Text, Title } = Typography;
const KEY = "model_multipliers";
const STAR = "*";
const multiplierKeys = [
  "input_multiplier",
  "output_multiplier",
  "cache_read_multiplier",
  "cache_write_multiplier",
] as const;

type Status = CreditRuleStatus | "all";
type RowState = "added" | "deleted" | "modified" | "unchanged";
type Usage = { input: number; output: number; cacheRead: number; cacheWrite: number };
type Row = {
  model_name: string;
  input_multiplier: number;
  output_multiplier: number;
  cache_read_multiplier: number;
  cache_write_multiplier: number;
};
type Diff = { model_name: string; before?: Row; after?: Row; state: RowState };

const defaultUsage: Usage = { input: 1000, output: 500, cacheRead: 0, cacheWrite: 0 };
const statusColor: Record<CreditRuleStatus, string> = { draft: "blue", active: "green", archived: "default" };
const diffColor: Record<RowState, string> = { added: "green", deleted: "red", modified: "gold", unchanged: "default" };
const diffClass: Record<RowState, string> = {
  added: "bg-emerald-50",
  deleted: "bg-rose-50",
  modified: "bg-amber-50",
  unchanged: "",
};

const effectNotice = [
  "配置调整仅对新 Usage 生效，不重算历史账本；",
  "真实扣费按后端 CreditRule 默认倍率 (*) 计算。",
].join("");
const actionNotice = [
  "该操作仅对新产生的 Usage 生效，不重算历史账本；",
  "已有 Usage 会继续保留当时冻结的倍率版本。",
].join("");
const editorNotice = [
  "只有默认兼容行 (*) 同步到后端 multiplier 字段并参与真实扣费；",
  "模型行仅保存到 metadata.model_multipliers。",
].join("");
const updatedAt = (rule: CodePlanCreditRule) => new Date(rule.updated_at ?? rule.created_at ?? 0).getTime();
const num = (value: unknown, fallback = 1) => (Number.isFinite(Number(value)) ? Number(value) : fallback);
const show = (value: number) =>
  Number.isInteger(value) ? String(value) : value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
const sortModelNames = (a: string, b: string) => {
  if (a === STAR) return -1;
  if (b === STAR) return 1;
  return a.localeCompare(b);
};
const sortRows = (a: Row, b: Row) => sortModelNames(a.model_name, b.model_name);
const clean = (row: Partial<Row>): Row => ({
  model_name: row.model_name?.trim() || STAR,
  input_multiplier: num(row.input_multiplier),
  output_multiplier: num(row.output_multiplier),
  cache_read_multiplier: num(row.cache_read_multiplier),
  cache_write_multiplier: num(row.cache_write_multiplier),
});
const rows = (items: Partial<Row>[] = []) => {
  const map = new Map<string, Row>();
  for (const item of items) {
    const row = clean(item);
    map.set(row.model_name, row);
  }
  if (!map.has(STAR)) map.set(STAR, clean({ model_name: STAR }));
  return [...map.values()].sort(sortRows);
};
const rowsOf = (rule: CodePlanCreditRule): Row[] => {
  const baseInput = {
    model_name: STAR,
    input_multiplier: rule.input_multiplier,
    output_multiplier: rule.output_multiplier,
    cache_read_multiplier: rule.cache_read_multiplier,
    cache_write_multiplier: rule.cache_write_multiplier,
  };
  const base = clean(baseInput);
  const raw = rule.metadata?.[KEY];
  if (!Array.isArray(raw)) return [base];
  const metadataRows = raw.filter((item) => item && typeof item === "object").map((item) => item as Partial<Row>);
  return rows([base, ...metadataRows]);
};
const latestRules = (items: CodePlanCreditRule[]) => {
  const byRuleId = new Map<string, CodePlanCreditRule>();
  for (const rule of items) {
    const current = byRuleId.get(rule.credit_rule_id);
    const isNewerVersion = current ? rule.version > current.version : true;
    const isNewerSameVersion = current
      ? rule.version === current.version && updatedAt(rule) > updatedAt(current)
      : false;
    if (isNewerVersion || isNewerSameVersion) {
      byRuleId.set(rule.credit_rule_id, rule);
    }
  }

  return [...byRuleId.values()].sort((left, right) => updatedAt(right) - updatedAt(left));
};
const label = (row?: Row) =>
  row
    ? `in ${show(row.input_multiplier)} / out ${show(row.output_multiplier)} / cache read ${show(
        row.cache_read_multiplier,
      )} / cache write ${show(row.cache_write_multiplier)}`
    : "-";
const calc = (row: Row | undefined, usage: Usage) =>
  row
    ? usage.input * row.input_multiplier +
      usage.output * row.output_multiplier +
      usage.cacheRead * row.cache_read_multiplier +
      usage.cacheWrite * row.cache_write_multiplier
    : 0;

function metadata(rule?: CodePlanCreditRule) {
  const value = { ...(rule?.metadata ?? {}) };
  delete value[KEY];
  return value;
}

function makePayload(
  name: string,
  description: string,
  editRows: Row[],
  metaText: string,
): CodePlanCreditRuleCreateRequest {
  const rowList = rows(editRows);
  for (const row of rowList) {
    const values = [row.input_multiplier, row.output_multiplier, row.cache_read_multiplier, row.cache_write_multiplier];
    if (values.some((value) => value < 0 || !Number.isFinite(value))) {
      throw new Error("倍率必须是大于等于 0 的数字");
    }
    if (!values.some((value) => value > 0)) throw new Error(`${row.model_name} 至少需要一个倍率大于 0`);
  }

  const meta = metaText.trim() ? JSON.parse(metaText) : {};
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new Error("metadata 必须是 JSON object");

  const defaultRow = rowList.find((row) => row.model_name === STAR) ?? rowList[0];
  return {
    name: name.trim(),
    description: description.trim() || null,
    input_multiplier: defaultRow.input_multiplier,
    output_multiplier: defaultRow.output_multiplier,
    cache_read_multiplier: defaultRow.cache_read_multiplier,
    cache_write_multiplier: defaultRow.cache_write_multiplier,
    metadata: { ...(meta as Record<string, unknown>), [KEY]: rowList },
  };
}

function pasteRows(text: string) {
  return rows(
    text
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .filter((line, index) => !(index === 0 && /^(model|model_name|模型)/i.test(line)))
      .map((line) => {
        const parts = line.includes("\t") ? line.split("\t") : line.split(/[,\s]+/);
        const [model_name, input_multiplier, output_multiplier, cache_read_multiplier, cache_write_multiplier] = parts;
        return {
          model_name,
          input_multiplier: num(input_multiplier),
          output_multiplier: num(output_multiplier),
          cache_read_multiplier: num(cache_read_multiplier),
          cache_write_multiplier: num(cache_write_multiplier ?? cache_read_multiplier),
        };
      }),
  );
}

function diffState(before?: Row, after?: Row): RowState {
  if (!before) return "added";
  if (!after) return "deleted";
  const beforeValues = [
    before.input_multiplier,
    before.output_multiplier,
    before.cache_read_multiplier,
    before.cache_write_multiplier,
  ].join("|");
  const afterValues = [
    after.input_multiplier,
    after.output_multiplier,
    after.cache_read_multiplier,
    after.cache_write_multiplier,
  ].join("|");
  return beforeValues === afterValues ? "unchanged" : "modified";
}

function diffRows(a: CodePlanCreditRule, b: CodePlanCreditRule): Diff[] {
  const left = new Map(rowsOf(a).map((row) => [row.model_name, row]));
  const right = new Map(rowsOf(b).map((row) => [row.model_name, row]));
  return [...new Set([...left.keys(), ...right.keys()])].sort(sortModelNames).map((model_name) => {
    const before = left.get(model_name);
    const after = right.get(model_name);
    return { model_name, before, after, state: diffState(before, after) };
  });
}

export default function CodePlanCreditRules() {
  const { accessToken, userRole } = useAuthorized();
  const [msg, holder] = message.useMessage();
  const [rules, setRules] = useState<CodePlanCreditRule[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [refs, setRefs] = useState<Record<string, CodePlanPlan[]>>({});
  const [status, setStatus] = useState<Status>("all");
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<CodePlanCreditRule | null>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [editRows, setEditRows] = useState<Row[]>(rows());
  const [meta, setMeta] = useState("{}");
  const [paste, setPaste] = useState("");
  const [previewId, setPreviewId] = useState("");
  const [previewModel, setPreviewModel] = useState(STAR);
  const [usage, setUsage] = useState<Usage>(defaultUsage);
  const [diffId, setDiffId] = useState("");
  const [from, setFrom] = useState(1);
  const [to, setTo] = useState(1);
  const [diff, setDiff] = useState<Diff[]>([]);

  const modelOptions = useMemo(() => [STAR, ...models], [models]);
  const ruleOptions = useMemo(() => rules.map((rule) => rule.credit_rule_id), [rules]);
  const previewRule = rules.find((rule) => rule.credit_rule_id === previewId) ?? rules[0];
  const previewRows = previewRule ? rowsOf(previewRule) : [];
  const previewBillingRow = previewRows.find((row) => row.model_name === STAR) ?? previewRows[0];
  const selectedMetadataRow = previewRows.find((row) => row.model_name === previewModel && row.model_name !== STAR);
  let previewNotice = "";
  if (previewModel !== STAR) {
    previewNotice = selectedMetadataRow
      ? "该模型行仅作为 metadata 保存；后端真实扣费当前仍使用 * 默认倍率。"
      : "未配置该模型行；后端真实扣费使用 * 默认倍率。";
  }

  const load = useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    try {
      const [ruleList, modelList, planList] = await Promise.all([
        listCodePlanCreditRules(accessToken, status === "all" ? undefined : { status }),
        listCodePlanAvailableModels(accessToken),
        listCodePlanPlans(accessToken, { status: "active" }),
      ]);
      const nextRules = latestRules(ruleList.data ?? []);
      const nextRefs: Record<string, CodePlanPlan[]> = {};
      for (const plan of planList.data ?? []) {
        if (plan.credit_rule_id) nextRefs[plan.credit_rule_id] = [...(nextRefs[plan.credit_rule_id] ?? []), plan];
      }
      const nextModels = [
        ...new Set((modelList.data ?? []).map((model) => model.model_name || model.id).filter(Boolean)),
      ].sort() as string[];

      setRules(nextRules);
      setRefs(nextRefs);
      setModels(nextModels);
      if (nextRules[0]) {
        const firstRule = nextRules[0];
        const previousVersion = Math.max(1, firstRule.version - 1);
        setPreviewId((current) => current || firstRule.credit_rule_id);
        setDiffId((current) => current || firstRule.credit_rule_id);
        setFrom((current) => (current === 1 ? previousVersion : current));
        setTo((current) => (current === 1 ? firstRule.version : current));
      }
    } catch (error) {
      msg.error(formatCodePlanError(error));
    } finally {
      setLoading(false);
    }
  }, [accessToken, msg, status]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const open = (rule?: CodePlanCreditRule) => {
    setEditing(rule ?? null);
    setName(rule?.name ?? "");
    setDescription(rule?.description ?? "");
    setEditRows(rule ? rowsOf(rule) : rows());
    setMeta(JSON.stringify(metadata(rule), null, 2));
    setShowEditor(true);
  };

  const save = async () => {
    if (!accessToken) return;
    try {
      const body = makePayload(name, description, editRows, meta);
      if (editing) {
        const patchBody = { ...body, version: editing.version } as CodePlanCreditRulePatchRequest;
        await updateCodePlanCreditRule(accessToken, editing.credit_rule_id, patchBody);
      } else {
        await createCodePlanCreditRule(accessToken, body);
      }
      msg.success(editing ? "已保存为新版本" : "已创建规则");
      setShowEditor(false);
      await load();
    } catch (error) {
      msg.error(error instanceof Error ? error.message : formatCodePlanError(error));
    }
  };

  const action = async (rule: CodePlanCreditRule, kind: "activate" | "archive") => {
    const usingPlans = refs[rule.credit_rule_id] ?? [];
    if (kind === "archive" && usingPlans.length) {
      const planNames = usingPlans.map((plan) => `${plan.name} (${plan.plan_id})`).join("、");
      msg.warning(`不能归档：仍有 active Plan 引用：${planNames}`);
      return;
    }
    const confirmed = window.confirm(actionNotice);
    if (!confirmed) {
      return;
    }
    try {
      if (kind === "activate") await activateCodePlanCreditRule(accessToken, rule.credit_rule_id, rule.version);
      else await archiveCodePlanCreditRule(accessToken, rule.credit_rule_id, rule.version);
      await load();
    } catch (error) {
      msg.error(formatCodePlanError(error));
    }
  };

  const loadDiff = async (id = diffId, a = from, b = to) => {
    if (!accessToken || !id) return;
    try {
      const [left, right] = await Promise.all([
        getCodePlanCreditRule(accessToken, id, a),
        getCodePlanCreditRule(accessToken, id, b),
      ]);
      setDiff(diffRows(left, right));
    } catch (error) {
      msg.error(formatCodePlanError(error));
    }
  };

  const setRow = (index: number, key: keyof Row, value: string | number) =>
    setEditRows((currentRows) =>
      currentRows.map((row, rowIndex) => {
        const nextValue = key === "model_name" ? String(value) : Number(value);
        return rowIndex === index ? clean({ ...row, [key]: nextValue }) : row;
      }),
    );

  if (!userRole || !isProxyAdminRole(userRole)) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <Title level={2}>计费规则</Title>
        <Alert type="error" showIcon message="需要 Proxy Admin 权限" />
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-7xl flex-col gap-5 p-4 lg:p-6">
      {holder}
      <div>
        <Text type="secondary">Code Plan / 计费规则</Text>
        <Title level={2} className="!mb-0">
          计费规则
        </Title>
        <Text>管理 CreditRule 默认倍率、metadata 模型行、预览计算和版本历史。</Text>
      </div>
      <Alert type="info" showIcon message={effectNotice} />

      <Card>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <select
            className="rounded border px-2 py-1"
            value={status}
            onChange={(event) => setStatus(event.target.value as Status)}
          >
            <option value="all">全部</option>
            <option value="draft">Draft</option>
            <option value="active">Active</option>
            <option value="archived">Archived</option>
          </select>
          <Button loading={loading} onClick={load}>
            刷新
          </Button>
          <Button type="primary" onClick={() => open()}>
            新建规则
          </Button>
          <Text type="secondary">共 {rules.length} 条当前规则</Text>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] text-left text-sm">
            <thead>
              <tr className="border-b text-slate-500">
                <th>规则</th>
                <th>状态</th>
                <th>版本</th>
                <th>模型行数</th>
                <th>Plan 引用</th>
                <th>默认倍率</th>
                <th>更新时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((rule) => {
                const referencedPlans = refs[rule.credit_rule_id] ?? [];
                const ruleRows = rowsOf(rule);
                const billingRow = ruleRows.find((row) => row.model_name === STAR) ?? ruleRows[0];
                const metadataRowCount = ruleRows.filter((row) => row.model_name !== STAR).length;
                const previousVersion = Math.max(1, rule.version - 1);
                return (
                  <tr key={`${rule.credit_rule_id}:${rule.version}`} className="border-b align-top">
                    <td className="py-2">
                      <Text strong>{rule.name}</Text>
                      <br />
                      <Text code>{rule.credit_rule_id}</Text>
                      <br />
                      <Text type="secondary">{rule.description}</Text>
                    </td>
                    <td>
                      <Tag color={statusColor[rule.status]}>{rule.status}</Tag>
                    </td>
                    <td>v{rule.version}</td>
                    <td>{metadataRowCount}</td>
                    <td>
                      <Tag color={referencedPlans.length ? "warning" : "default"}>{referencedPlans.length}</Tag>
                    </td>
                    <td>{label(billingRow)}</td>
                    <td>{rule.updated_at ? new Date(rule.updated_at).toLocaleString() : "-"}</td>
                    <td>
                      <Button size="small" disabled={rule.status === "archived"} onClick={() => open(rule)}>
                        编辑
                      </Button>{" "}
                      <Button
                        size="small"
                        onClick={() => {
                          setDiffId(rule.credit_rule_id);
                          setFrom(previousVersion);
                          setTo(rule.version);
                          void loadDiff(rule.credit_rule_id, previousVersion, rule.version);
                        }}
                      >
                        diff
                      </Button>{" "}
                      {rule.status === "draft" && (
                        <Button size="small" type="primary" onClick={() => action(rule, "activate")}>
                          激活
                        </Button>
                      )}{" "}
                      {rule.status !== "archived" && (
                        <Button size="small" danger onClick={() => action(rule, "archive")}>
                          归档
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!rules.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无计费规则" />}
        </div>
      </Card>

      {showEditor && (
        <Card title={editing ? `编辑 ${editing.credit_rule_id}` : "新建 CreditRule"}>
          <Alert className="mb-3" type="info" showIcon message={editorNotice} />
          <div className="grid gap-3">
            <input
              className="rounded border px-3 py-2"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="规则名"
            />
            <textarea
              className="rounded border px-3 py-2"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="描述"
            />
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-sm">
                <thead>
                  <tr className="text-slate-500">
                    <th>模型</th>
                    <th>Input</th>
                    <th>Output</th>
                    <th>Cache read</th>
                    <th>Cache write</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {editRows.map((row, index) => (
                    <tr key={row.model_name || index}>
                      <td>
                        <input
                          list="models"
                          className="w-full rounded border px-2 py-1"
                          value={row.model_name}
                          onChange={(event) => setRow(index, "model_name", event.target.value)}
                        />
                      </td>
                      {multiplierKeys.map((key) => (
                        <td key={key}>
                          <input
                            type="number"
                            min={0}
                            step={0.01}
                            className="w-full rounded border px-2 py-1"
                            value={row[key]}
                            onChange={(event) => setRow(index, key, event.target.value)}
                          />
                        </td>
                      ))}
                      <td>
                        <Button
                          danger
                          disabled={editRows.length <= 1}
                          onClick={() =>
                            setEditRows((currentRows) => currentRows.filter((_, rowIndex) => rowIndex !== index))
                          }
                        >
                          -
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <datalist id="models">
              {modelOptions.map((model) => (
                <option key={model} value={model} />
              ))}
            </datalist>
            <div>
              <Button onClick={() => setEditRows((currentRows) => [...currentRows, clean({ model_name: "" })])}>
                添加模型行
              </Button>
            </div>
            <textarea
              className="rounded border px-3 py-2"
              rows={3}
              value={paste}
              onChange={(event) => setPaste(event.target.value)}
              placeholder="批量粘贴：model input output cache_read cache_write"
            />
            <Button
              disabled={!paste.trim()}
              onClick={() => {
                setEditRows((currentRows) => rows([...currentRows, ...pasteRows(paste)]));
                setPaste("");
              }}
            >
              导入粘贴内容
            </Button>
            <textarea
              className="rounded border px-3 py-2 font-mono text-xs"
              rows={5}
              value={meta}
              onChange={(event) => setMeta(event.target.value)}
              placeholder="metadata JSON"
            />
            <div className="flex justify-end gap-2">
              <Button onClick={() => setShowEditor(false)}>取消</Button>
              <Button type="primary" onClick={save}>
                {editing ? "保存为新版本" : "创建"}
              </Button>
            </div>
          </div>
        </Card>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="换算预览">
          <div className="grid gap-3">
            <select
              className="rounded border px-2 py-1"
              value={previewRule?.credit_rule_id ?? ""}
              onChange={(event) => setPreviewId(event.target.value)}
            >
              {ruleOptions.map((id) => (
                <option key={id}>{id}</option>
              ))}
            </select>
            <select
              className="rounded border px-2 py-1"
              value={previewModel}
              onChange={(event) => setPreviewModel(event.target.value)}
            >
              {modelOptions.map((model) => (
                <option key={model}>{model}</option>
              ))}
            </select>
            <div className="grid grid-cols-2 gap-2">
              {(["input", "output", "cacheRead", "cacheWrite"] as const).map((key) => (
                <input
                  key={key}
                  type="number"
                  min={0}
                  className="rounded border px-2 py-1"
                  value={usage[key]}
                  onChange={(event) =>
                    setUsage((currentUsage) => ({ ...currentUsage, [key]: num(event.target.value, 0) }))
                  }
                  placeholder={key}
                />
              ))}
            </div>
            <div className="rounded bg-slate-50 p-3">
              <Text type="secondary">
                公式：input_tokens x input_multiplier + output_tokens x output_multiplier + cache_read_tokens x
                cache_read_multiplier + cache_write_tokens x cache_write_multiplier
              </Text>
              <div className="mt-2 text-2xl font-semibold">{show(calc(previewBillingRow, usage))} credits</div>
              <Text type="secondary">后端计费倍率：{label(previewBillingRow)}</Text>
              {selectedMetadataRow && (
                <>
                  <br />
                  <Text type="secondary">metadata 模型行：{label(selectedMetadataRow)}</Text>
                </>
              )}
              {previewNotice && <Alert className="mt-3" type="warning" showIcon message={previewNotice} />}
            </div>
          </div>
        </Card>
        <Card title="版本历史 diff">
          <div className="mb-3 grid gap-2 md:grid-cols-[1fr_90px_90px_auto]">
            <select
              className="rounded border px-2 py-1"
              value={diffId}
              onChange={(event) => setDiffId(event.target.value)}
            >
              {ruleOptions.map((id) => (
                <option key={id}>{id}</option>
              ))}
            </select>
            <input
              type="number"
              min={1}
              className="rounded border px-2 py-1"
              value={from}
              onChange={(event) => setFrom(num(event.target.value, 1))}
            />
            <input
              type="number"
              min={1}
              className="rounded border px-2 py-1"
              value={to}
              onChange={(event) => setTo(num(event.target.value, 1))}
            />
            <Button onClick={() => loadDiff()}>对比</Button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b text-slate-500">
                  <th>模型</th>
                  <th>v{from}</th>
                  <th>v{to}</th>
                  <th>变化</th>
                </tr>
              </thead>
              <tbody>
                {diff.map((item) => (
                  <tr key={item.model_name} className={diffClass[item.state]}>
                    <td>
                      <Text code={item.model_name === STAR}>{item.model_name}</Text>
                    </td>
                    <td>{label(item.before)}</td>
                    <td>{label(item.after)}</td>
                    <td>
                      <Tag color={diffColor[item.state]}>{item.state}</Tag>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!diff.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择规则和版本后点击对比" />}
          </div>
        </Card>
      </div>
    </div>
  );
}
