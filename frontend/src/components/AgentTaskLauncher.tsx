import { useEffect, useState } from "react";
import { createAgentRun } from "../api/client";
import { useAgentProviders, useAgentTasks } from "../hooks/useAgentTasks";
import { useScore } from "../hooks/useScore";
import { Term } from "./ui/Term";

interface AgentTaskLauncherProps {
  projectId: string;
  onRunStarted: (runId: string) => void;
}

const INVESTIGATE_TASK_ID = "investigate";

/**
 * #51: 標準タスク選択 or 自然言語入力でL2エージェントrunを起動する(FR-21/UC-4)。
 *
 * `task_type="investigate"`は自然言語の自由指示が必須(`AgentRunManager`の
 * `MissingPromptError`、バックエンドと同じくフロントエンドでもタスクIDを
 * ハードコードして判定する)。`requires_scope=True`のタスク(voicing-fix等)は
 * `TaskDefinitionResponse.requires_scope`(#51で追加公開)を見て、スコープ未指定
 * なら送信前に止める。
 */
export function AgentTaskLauncher({ projectId, onRunStarted }: AgentTaskLauncherProps) {
  const { tasks } = useAgentTasks();
  const { providers } = useAgentProviders();
  const { data: score } = useScore(projectId);
  const parts = score?.parts ?? [];

  const [taskType, setTaskType] = useState<string>("");
  const [prompt, setPrompt] = useState("");
  const [partId, setPartId] = useState("");
  const [barStart, setBarStart] = useState("");
  const [barEnd, setBarEnd] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [budget, setBudget] = useState("");
  const [isLaunching, setIsLaunching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (tasks.length > 0 && !tasks.some((t) => t.id === taskType)) {
      setTaskType(tasks[0].id);
    }
  }, [tasks, taskType]);

  // Gate2レビュー指摘(LOW): providerの初期値を"claude"に固定すると、一覧に
  // claudeが無い/configured=falseの場合でも<select>の値だけがそのまま残り、
  // disabledなoptionを選んだ状態のまま送信できてしまう(HTMLのdisabled optionは
  // 選択済みの値を消さない)。providers取得後、現在の選択が未設定(未取得)か
  // configuredでない場合は最初のconfigured providerへ切り替える。
  useEffect(() => {
    if (providers.length === 0) return;
    const current = providers.find((p) => p.name === provider);
    if (current?.configured) return;
    const firstConfigured = providers.find((p) => p.configured);
    if (firstConfigured) setProvider(firstConfigured.name);
  }, [providers, provider]);

  const selectedTask = tasks.find((t) => t.id === taskType);
  const isInvestigate = taskType === INVESTIGATE_TASK_ID;
  const scopeRequired = selectedTask?.requires_scope === true;
  const selectedProvider = providers.find((p) => p.name === provider);
  const providerReady = selectedProvider?.configured === true;

  async function handleLaunch() {
    setError(null);
    if (!providerReady) {
      setError("選択中のプロバイダは未設定です。設定済みのプロバイダを選択してください。");
      return;
    }
    if (isInvestigate && prompt.trim().length === 0) {
      setError("自然言語での指示を入力してください(このタスクは指示が必須です)。");
      return;
    }
    if (scopeRequired && !partId) {
      setError("対象パートを選択してください(このタスクは対象範囲の指定が前提です)。");
      return;
    }

    const scope: Record<string, unknown> | null = partId
      ? {
          part_id: partId,
          bar_range: barStart && barEnd ? [Number(barStart), Number(barEnd)] : null,
        }
      : null;

    setIsLaunching(true);
    try {
      const { run_id } = await createAgentRun(projectId, {
        task_type: taskType,
        scope,
        prompt: prompt.trim().length > 0 ? prompt.trim() : null,
        provider,
        model: model.trim().length > 0 ? model.trim() : null,
        budget: budget.trim().length > 0 ? Number(budget) : null,
      });
      onRunStarted(run_id);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setIsLaunching(false);
    }
  }

  return (
    <section className="space-y-4 rounded-lg border border-gray-200 dark:border-gray-700 p-4">
      <div>
        <h3 className="text-lg font-semibold text-gray-700 dark:text-gray-200">
          <Term k="agent">AI</Term>に修正を頼む
        </h3>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          用意された作業内容から選ぶか、自然言語で調査・修正を依頼できます
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
          タスク
          <select
            value={taskType}
            onChange={(e) => setTaskType(e.target.value)}
            disabled={isLaunching || tasks.length === 0}
            className="rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          >
            {tasks.map((t) => (
              <option key={t.id} value={t.id}>
                {t.id} — {t.purpose}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
          プロバイダ
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            disabled={isLaunching}
            className="rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          >
            {providers.map((p) => (
              <option key={p.name} value={p.name} disabled={!p.configured}>
                {p.name}
                {!p.configured ? " (未設定)" : ""}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
          モデル(任意)
          <input
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={isLaunching}
            placeholder="既定"
            className="w-32 rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          />
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
          <Term k="tokenBudget">処理量の上限</Term>(任意)
          <input
            type="number"
            min={1}
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            disabled={isLaunching}
            placeholder="タスク既定値"
            className="w-32 rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          />
        </label>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
          対象パート{scopeRequired ? "(必須)" : "(任意)"}
          <select
            value={partId}
            onChange={(e) => setPartId(e.target.value)}
            disabled={isLaunching}
            className="rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          >
            <option value="">(全体)</option>
            {parts.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.id})
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
          開始小節
          <input
            type="number"
            min={1}
            value={barStart}
            onChange={(e) => setBarStart(e.target.value)}
            disabled={isLaunching || !partId}
            className="w-24 rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          />
        </label>

        <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
          終了小節
          <input
            type="number"
            min={1}
            value={barEnd}
            onChange={(e) => setBarEnd(e.target.value)}
            disabled={isLaunching || !partId}
            className="w-24 rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
          />
        </label>
      </div>

      <label className="flex flex-col gap-1 text-sm text-gray-600 dark:text-gray-300">
        自然言語での指示
        {isInvestigate ? "(必須)" : "(任意・タスクの目的に追加する補足指示)"}
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          disabled={isLaunching}
          rows={3}
          placeholder="例: 12〜20小節の左手の声部割り当てがおかしい。調べて直して"
          className="rounded-md border border-gray-300 dark:border-gray-600 px-2 py-1 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500"
        />
      </label>

      <button
        type="button"
        onClick={() => void handleLaunch()}
        disabled={isLaunching || !taskType || !providerReady}
        className="rounded-md bg-indigo-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-500 disabled:opacity-50"
      >
        {isLaunching ? "起動中..." : "AIに依頼する"}
      </button>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
    </section>
  );
}
