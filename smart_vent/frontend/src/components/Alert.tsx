import type { ReactNode } from "react";

export type AlertVariant = "info" | "success" | "warning" | "danger";

interface AlertProps {
  variant: AlertVariant;
  children: ReactNode;
  /** Optional buttons/links rendered in a compact row beneath the body. */
  actions?: ReactNode;
  /** Optional ``data-testid`` so tests can locate the alert without colour-matching. */
  testId?: string;
  /** Forwarded to the underlying element — useful for spacing in dense layouts. */
  className?: string;
}

/**
 * Bootstrap-style banner utility (#213).
 *
 * Used by the airflow-floor upgrade banner and the migrated vacation-mode,
 * unit-change, and stale-sensor banners. The classes come from styles.css —
 * adding a UI library just for this would be overkill, but the previous
 * "vacation-mode-banner" plain-text styling was not visually distinct enough
 * for a safety-relevant notice, so we standardise on a real alert vocabulary.
 */
export default function Alert({ variant, children, actions, testId, className }: AlertProps) {
  const classes = ["alert", `alert-${variant}`, className].filter(Boolean).join(" ");
  return (
    <div className={classes} role="alert" data-testid={testId}>
      {children}
      {actions && <div className="alert-actions">{actions}</div>}
    </div>
  );
}
