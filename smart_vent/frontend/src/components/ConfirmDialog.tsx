import { createPortal } from "react-dom";

/**
 * Shared destructive-action confirmation modal. Portals to document.body so
 * it renders correctly regardless of the caller's DOM context (e.g. a table
 * row, where a plain nested <div> would be invalid HTML).
 */
export default function ConfirmDialog({
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return createPortal(
    <div
      className="modal-backdrop"
      data-testid="confirm-dialog"
      onClick={(e) => e.target === e.currentTarget && onCancel()}
    >
      <div className="modal" style={{ maxWidth: 440 }}>
        <div className="modal-title">{title}</div>
        <p style={{ color: "var(--gray-700)", marginBottom: "1.5rem", whiteSpace: "pre-line" }}>
          {message}
        </p>
        <div className="modal-footer">
          <button className="btn btn-secondary" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button className="btn btn-danger" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
