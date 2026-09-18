import { useState } from 'react'
import {
  useAddModelKey,
  useDeleteModelKey,
  useModelConfig,
  useSetModel,
  useTestModelKeys,
} from '../api/hooks'
import { Card } from '../components/Card'
import { Badge } from '../components/Badge'
import { ConfirmDialog } from '../components/ConfirmDialog'
import type {
  KeyPoolGroup,
  KeyPoolStatus,
  KeyStatusEntry,
  MimoChannel,
  FixedModelGroupStatus,
  ModelGroupStatus,
  ModelKeyTestResult,
  OtherServiceKey,
  PrimaryTravelModelStatus,
} from '../api/types'

export function ModelConfigPage() {
  const { data, isLoading, error } = useModelConfig()

  if (isLoading) return <p className="text-ink-3">加载中…</p>
  if (error) return <p className="text-danger">加载失败：{(error as Error).message}</p>
  if (!data) return null

  return (
    <div className="flex flex-col gap-5">
      <PrimaryTravelCard route={data.primary_travel} />
      <ModelGroupCard group={data.groups.full} />
      <ModelGroupCard group={data.groups.light} />
      <FixedModelGroupCard group={data.groups.cunai} />
      <FixedModelGroupCard group={data.groups.mimo} requireChannel />
      <KeyPoolCard pool={data.key_pools.tavily} />
      <KeyPoolCard pool={data.key_pools.poster} />
      <OtherServicesCard services={data.other_services} />
    </div>
  )
}

function PrimaryTravelCard({ route }: { route: PrimaryTravelModelStatus }) {
  return (
    <Card className="p-5 border-brand">
      <div className="flex items-center justify-between mb-1">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide">{route.label}</div>
        <Badge variant={route.configured ? 'ok' : 'pending'}>{route.provider_group}</Badge>
      </div>
      <div className="flex items-center gap-3 mt-2 flex-wrap">
        <span className="text-lg font-bold">{route.provider}</span>
        <span className="text-sm font-bold font-mono">{route.model}</span>
      </div>
      <div className="mt-2 text-xs font-mono text-ink-2 break-all">{route.base_url}</div>
      <p className="text-xs text-ink-3 mt-2">{route.usage}</p>
      <p className="text-xs text-ink-3 mt-1">
        由 TRAVEL_MODEL_PROVIDER 决定；当前{route.configured ? `已配置 ${route.key_count} 个 Key` : '未配置可用 Key'}。
        下方卡片是兼容分组与 Key 管理入口，不代表主链顺序。
      </p>
    </Card>
  )
}

/** 共享的"测试结果"状态+触发逻辑，ModelGroupCard/MimoGroupCard/KeyPoolCard三种卡片
 * 都需要（key: index -> 最近一次测试结果，单个key测试/"测试全部"都往这个map里合并写，
 * 不会互相清空对方已经测过的结果）。*/
function useKeyTestResults(group: KeyPoolGroup) {
  const testMutation = useTestModelKeys()
  const [testResults, setTestResults] = useState<Map<number, ModelKeyTestResult>>(new Map())

  const runTest = (keyIndex?: number) => {
    testMutation.mutate(
      { group, keyIndex },
      {
        onSuccess: (data) => {
          setTestResults((prev) => {
            const next = new Map(prev)
            for (const r of data.results) next.set(r.index, r)
            return next
          })
        },
      },
    )
  }

  return { testMutation, testResults, runTest }
}

/** 单个key行：脱敏展示 + 测试按钮 + 删除按钮（删除走共享ConfirmDialog二次确认）。*/
function KeyRow({
  group,
  entry,
  result,
  testing,
  onTest,
}: {
  group: KeyPoolGroup
  entry: KeyStatusEntry
  result: ModelKeyTestResult | undefined
  testing: boolean
  onTest: () => void
}) {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const deleteKey = useDeleteModelKey()

  return (
    <div
      className={`flex items-center gap-3 px-3.5 py-2.5 rounded-lg border ${
        entry.is_current ? 'border-brand bg-brand-soft' : 'border-border bg-surface'
      }`}
    >
      <span className="text-xs text-ink-3 font-mono">#{entry.index}</span>
      <span className="font-mono text-[13px]">{entry.masked}</span>
      {entry.is_current && <Badge variant="brand">当前使用</Badge>}
      <div className="flex-1" />
      {result && (
        <span
          className={`text-xs ${result.ok === true ? 'text-ok' : result.ok === false ? 'text-danger' : 'text-ink-3'}`}
          title={result.error ?? undefined}
        >
          {result.ok === true
            ? `可用 · ${result.latency_ms}ms`
            : result.ok === false
              ? `不可用：${result.error}`
              : result.error}
        </span>
      )}
      <button
        className="text-[12px] font-semibold px-2.5 py-1 rounded-md border border-border-strong text-ink-2 hover:bg-bg-inset disabled:opacity-50"
        disabled={testing}
        onClick={onTest}
      >
        测试
      </button>
      <button
        className="text-[12px] font-semibold px-2.5 py-1 rounded-md border border-danger text-danger hover:bg-danger-soft"
        onClick={() => setConfirmOpen(true)}
      >
        删除
      </button>
      {confirmOpen && (
        <ConfirmDialog
          title="确定要删除这个Key吗？"
          message="删除后该Key立即停用，如需恢复请重新添加。"
          confirmLabel="确定删除"
          onCancel={() => setConfirmOpen(false)}
          onConfirm={() => {
            setConfirmOpen(false)
            deleteKey.mutate({ group, index: entry.index })
          }}
        />
      )}
    </div>
  )
}

/** 新增key的输入行——mimo分组需要额外的channel选择器，
 * 其余分组只是一个文本输入+添加按钮。*/
function AddKeyRow({ group, requireChannel }: { group: KeyPoolGroup; requireChannel?: boolean }) {
  const addKey = useAddModelKey()
  const [draft, setDraft] = useState('')
  const [channel, setChannel] = useState<MimoChannel>('gptload')

  const submit = () => {
    if (!draft.trim()) return
    addKey.mutate(
      { group, key: draft.trim(), channel: requireChannel ? channel : undefined },
      { onSuccess: () => setDraft('') },
    )
  }

  return (
    <div className="flex gap-2 mt-3">
      {requireChannel && (
        <select
          className="border border-border rounded-lg px-2 py-2 text-sm bg-surface"
          value={channel}
          onChange={(e) => setChannel(e.target.value as MimoChannel)}
        >
          <option value="gptload">gptload（聚合优先）</option>
          <option value="token_plan">token_plan</option>
          <option value="legacy">official / legacy</option>
        </select>
      )}
      <input
        className="flex-1 border border-border rounded-lg px-3 py-2 text-sm"
        placeholder="输入新Key"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <button
        className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-brand text-white disabled:opacity-50"
        disabled={addKey.isPending || !draft.trim()}
        onClick={submit}
      >
        添加
      </button>
    </div>
  )
}

function ModelGroupCard({ group }: { group: ModelGroupStatus }) {
  const { mutate: setModel, isPending } = useSetModel()
  const [draft, setDraft] = useState('')
  const { testMutation, testResults, runTest } = useKeyTestResults(group.group)

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between mb-1">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide">{group.label}</div>
        <Badge variant={group.group === 'full' ? 'brand' : 'pending'}>{group.group}</Badge>
      </div>
      <div className="flex items-center gap-3 mt-2">
        <span className="text-lg font-bold">{group.effective_model}</span>
        {group.override_active && <Badge variant="brand">运行时已切换</Badge>}
      </div>
      <p className="text-xs text-ink-3 mt-2">{group.usage}</p>
      <p className="text-xs text-ink-3 mt-2">
        默认值（.env）：{group.default_model}。运行时切换只在当前API进程内生效，不影响独立CLI进程
        （如 run_mvp.py），重启API进程后会回退到默认值。
      </p>

      <div className="flex gap-2 mt-4">
        <input
          className="flex-1 border border-border rounded-lg px-3 py-2 text-sm"
          placeholder={`输入新模型名，如 ${group.default_model}`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button
          className="px-4 py-2 rounded-lg text-[13px] font-semibold bg-brand text-white disabled:opacity-50"
          disabled={isPending || !draft.trim()}
          onClick={() => setModel({ model: draft.trim(), group: group.group })}
        >
          切换
        </button>
        <button
          className="px-4 py-2 rounded-lg text-[13px] font-semibold border border-border-strong text-ink-2 disabled:opacity-50"
          disabled={isPending || !group.override_active}
          onClick={() => setModel({ model: null, group: group.group })}
        >
          恢复默认
        </button>
      </div>

      <div className="flex items-center justify-between mt-5 mb-2.5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide">
          API Key（脱敏展示，共 {group.key_count} 个，独立轮询指针）
        </div>
        <button
          className="text-[12px] font-semibold px-3 py-1.5 rounded-lg border border-border-strong text-ink-2 hover:bg-bg-inset disabled:opacity-50"
          disabled={testMutation.isPending || group.key_count === 0}
          onClick={() => runTest(undefined)}
        >
          {testMutation.isPending ? '测试中…' : '测试全部Key'}
        </button>
      </div>
      <div className="flex flex-col gap-2">
        {group.keys.map((k) => (
          <KeyRow
            key={k.index}
            group={group.group}
            entry={k}
            result={testResults.get(k.index)}
            testing={testMutation.isPending}
            onTest={() => runTest(k.index)}
          />
        ))}
        {group.keys.length === 0 && <p className="text-sm text-ink-3">未配置任何Key。</p>}
      </div>
      <AddKeyRow group={group.group} />
    </Card>
  )
}

function FixedModelGroupCard({
  group,
  requireChannel = false,
}: {
  group: FixedModelGroupStatus
  requireChannel?: boolean
}) {
  const { testMutation, testResults, runTest } = useKeyTestResults(group.group)

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between mb-1">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide">{group.label}</div>
        <Badge variant="pending">{group.group}</Badge>
      </div>
      <div className="flex items-center gap-3 mt-2">
        {group.available_models.map((model) => (
          <span key={model} className="text-sm font-bold font-mono">{model}</span>
        ))}
      </div>
      <p className="text-xs text-ink-3 mt-2">{group.usage}</p>

      <div className="flex items-center justify-between mt-5 mb-2.5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide">
          API Key（脱敏展示，共 {group.key_count} 个）
        </div>
        <button
          className="text-[12px] font-semibold px-3 py-1.5 rounded-lg border border-border-strong text-ink-2 hover:bg-bg-inset disabled:opacity-50"
          disabled={testMutation.isPending || group.key_count === 0}
          onClick={() => runTest(undefined)}
        >
          {testMutation.isPending ? '测试中…' : '测试全部Key'}
        </button>
      </div>
      <div className="flex flex-col gap-2">
        {group.keys.map((k) => (
          <KeyRow
            key={k.index}
            group={group.group}
            entry={k}
            result={testResults.get(k.index)}
            testing={testMutation.isPending}
            onTest={() => runTest(k.index)}
          />
        ))}
        {group.keys.length === 0 && <p className="text-sm text-ink-3">未配置任何Key。</p>}
      </div>
      <AddKeyRow group={group.group} requireChannel={requireChannel} />
    </Card>
  )
}

function KeyPoolCard({ pool }: { pool: KeyPoolStatus }) {
  const { testMutation, testResults, runTest } = useKeyTestResults(pool.group)

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between mb-1">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide">{pool.label}</div>
        <Badge variant="pending">{pool.group}</Badge>
      </div>
      <p className="text-xs text-ink-3 mt-2">{pool.usage}</p>

      <div className="flex items-center justify-between mt-5 mb-2.5">
        <div className="text-xs font-bold text-ink-3 uppercase tracking-wide">
          API Key（脱敏展示，共 {pool.key_count} 个）
        </div>
        <button
          className="text-[12px] font-semibold px-3 py-1.5 rounded-lg border border-border-strong text-ink-2 hover:bg-bg-inset disabled:opacity-50"
          disabled={testMutation.isPending || pool.key_count === 0}
          onClick={() => runTest(undefined)}
        >
          {testMutation.isPending ? '测试中…' : '测试全部Key'}
        </button>
      </div>
      <div className="flex flex-col gap-2">
        {pool.keys.map((k) => (
          <KeyRow
            key={k.index}
            group={pool.group}
            entry={k}
            result={testResults.get(k.index)}
            testing={testMutation.isPending}
            onTest={() => runTest(k.index)}
          />
        ))}
        {pool.keys.length === 0 && <p className="text-sm text-ink-3">未配置任何Key。</p>}
      </div>
      <AddKeyRow group={pool.group} />
    </Card>
  )
}

function OtherServicesCard({ services }: { services: OtherServiceKey[] }) {
  return (
    <Card className="p-5">
      <div className="text-xs font-bold text-ink-3 uppercase tracking-wide mb-1">
        其他服务 Key（仅只读展示，脱敏）
      </div>
      <p className="text-xs text-ink-3 mb-3">
        高德地图/Embedding这几类服务架构上是单一全局凭证，不支持网页表单增删改，
        新增/编辑仍然只能走 .env 文件。
      </p>
      <div className="flex flex-col gap-2.5">
        {services.map((s) => (
          <div key={s.name} className="border border-border rounded-lg p-3.5">
            <div className="flex items-center justify-between">
              <b className="text-[13.5px]">{s.name}</b>
              <Badge variant={s.configured ? 'ok' : 'pending'}>{s.configured ? '已配置' : '未配置'}</Badge>
            </div>
            <p className="text-xs text-ink-3 mt-1">{s.usage}</p>
            {s.masked_keys.length > 0 && (
              <div className="flex gap-1.5 flex-wrap mt-2">
                {s.masked_keys.map((k, i) => (
                  <span key={i} className="text-[11px] px-2 py-0.5 rounded-full bg-bg-inset border border-border font-mono text-ink-2">
                    {k}
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </Card>
  )
}
