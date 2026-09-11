export default function RouterModelPicker({
  models = [],
  selectedId,
  defaultModelId,
  disabled = false,
  error = "",
  onChange,
}) {
  const options = Array.isArray(models) ? models : [];
  return (
    <label className="router-model-picker">
      <span className="router-model-picker__label">對話模型</span>
      <select aria-label="對話模型"
        value={selectedId ?? ""}
        disabled={disabled || options.length === 0}
        onChange={(event) => {
          const next = Number(event.target.value);
          if (Number.isFinite(next) && typeof onChange === "function") onChange(next);
        }}
      >
        {options.length === 0 && <option value="">沒有可用模型</option>}
        {options.map((model) => (
          <option key={model.id} value={model.id}>
            {model.display_name || model.name}
            {model.id === defaultModelId ? "（全院預設）" : ""}
            {model.health_status && model.health_status !== "healthy" ? ` · ${model.health_status}` : ""}
          </option>
        ))}
      </select>
      {error ? <span className="router-model-picker__error">{error}</span> : null}
    </label>
  );
}
