import { useEffect } from "react";

interface Props {
  title: string;
  lines: string[];
  actionLabel: string;
  onCancel: () => void;
  onConfirm: () => void;
}

/**
 * 重操作的确认框（删账号、清空归档）。
 * 内容级删除不走这里 —— 那条路径用「撤销」提示，批量清理时不会磕手。
 */
export function ConfirmDialog({ title, lines, actionLabel, onCancel, onConfirm }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="modal-mask" onClick={onCancel} role="presentation">
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <h3>{title}</h3>
        <ul className="modal-lines">
          {lines.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
        <div className="modal-actions">
          <button className="secondary-btn" onClick={onCancel}>
            取消
          </button>
          <button className="danger-btn" onClick={onConfirm} autoFocus>
            {actionLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
