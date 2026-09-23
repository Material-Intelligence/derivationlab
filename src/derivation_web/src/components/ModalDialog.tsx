/* eslint-disable jsx-a11y/no-noninteractive-element-interactions -- native dialog owns cancel and backdrop pointer events. */
import { useEffect, useRef, type ReactNode } from "react";

const FOCUSABLE = "button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], summary, [tabindex]:not([tabindex='-1'])";

interface ModalDialogProps {
  labelledBy: string;
  variant?: "centered" | "drawer";
  dismissible?: boolean;
  closeOnBackdrop?: boolean;
  onClose: () => void;
  children: ReactNode;
}

export function ModalDialog({
  labelledBy,
  variant = "centered",
  dismissible = true,
  closeOnBackdrop = true,
  onClose,
  children,
}: ModalDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) dialog.showModal();
    const initial = dialog.querySelector<HTMLElement>("[data-modal-initial-focus]")
      ?? dialog.querySelector<HTMLElement>(FOCUSABLE);
    initial?.focus();
    return () => {
      if (dialog.open) dialog.close();
      restoreFocusRef.current?.focus();
    };
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className={`modal-layer ${variant}`}
      aria-labelledby={labelledBy}
      onCancel={(event) => {
        event.preventDefault();
        if (dismissible) onClose();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const focusable = [...event.currentTarget.querySelectorAll<HTMLElement>(FOCUSABLE)]
          .filter((element) => !element.hasAttribute("disabled") && element.getClientRects().length > 0);
        const first = focusable[0];
        const last = focusable.at(-1);
        if (!first || !last) {
          event.preventDefault();
          event.currentTarget.focus();
          return;
        }
        const active = document.activeElement;
        if (event.shiftKey && (active === first || !event.currentTarget.contains(active))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (active === last || !event.currentTarget.contains(active))) {
          event.preventDefault();
          first.focus();
        }
      }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && dismissible && closeOnBackdrop) onClose();
      }}
    >
      {children}
    </dialog>
  );
}
