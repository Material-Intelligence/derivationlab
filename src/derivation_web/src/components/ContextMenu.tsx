import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

export type MenuIconName = "open" | "pin" | "export" | "copy" | "folder" | "details" | "center" | "route" | "branch" | "fit" | "reset" | "settings" | "check";

export interface ContextMenuAction {
  id: string;
  label: string;
  icon: MenuIconName;
  disabled?: boolean;
  separatorBefore?: boolean;
  /** Heading for the group this entry opens, rendered above it. */
  groupLabel?: string;
  radio?: boolean;
  checked?: boolean;
  onSelect: () => void | Promise<void>;
}

export interface ContextMenuState {
  x: number;
  y: number;
  label: string;
  groupLabel?: string;
  placement?: "point" | "above-start";
  actions: ContextMenuAction[];
  restoreFocus?: HTMLElement | SVGElement | null;
}

interface ContextMenuProps {
  menu: ContextMenuState | null;
  onClose: () => void;
}

const iconPaths: Record<MenuIconName, ReactNode> = {
  open: <><path d="M5 4.5h4l1.5 2H15v9H5z" /><path d="M5 8h10" /></>,
  pin: <><path d="m7 4 6 6M10 5l3-1 1 1-1 3-4 4-3-3z" /><path d="m8 11-3 4" /></>,
  export: <><path d="M5 11v4h10v-4" /><path d="M10 3v9M7 9l3 3 3-3" /></>,
  copy: <><rect x="6" y="6" width="9" height="9" rx="1" /><path d="M12 6V4H4v8h2" /></>,
  folder: <><path d="M3.5 5.5h5l1.5 2h6.5v8h-13z" /><path d="M3.5 8h13" /></>,
  details: <><circle cx="10" cy="10" r="6.5" /><path d="M10 9v4M10 6.5v.5" /></>,
  center: <><path d="M4 7V4h3M13 4h3v3M16 13v3h-3M7 16H4v-3" /><circle cx="10" cy="10" r="2" /></>,
  route: <><circle cx="5" cy="5" r="1.5" /><circle cx="15" cy="15" r="1.5" /><path d="M6.5 5c5 0 1.5 10 7 10" /></>,
  branch: <><path d="M6 4v12M6 8c4 0 3-4 7-4M6 11c4 0 3 4 7 4" /><circle cx="13.5" cy="4" r="1" /><circle cx="13.5" cy="15" r="1" /></>,
  fit: <><path d="M4 8V4h4M12 4h4v4M16 12v4h-4M8 16H4v-4" /></>,
  reset: <><path d="M5 7a6 6 0 1 1-.4 5" /><path d="M4 4v4h4" /></>,
  settings: <><circle cx="10" cy="10" r="2.4" /><path d="M10 3.2v2M10 14.8v2M3.2 10h2M14.8 10h2M5.2 5.2l1.4 1.4M13.4 13.4l1.4 1.4M14.8 5.2l-1.4 1.4M6.6 13.4l-1.4 1.4" /></>,
  check: <path d="m5.5 10.2 3 3 6-6.4" />,
};

export function MenuIcon({ name }: { name: MenuIconName }) {
  return <svg className="context-menu-icon" viewBox="0 0 20 20" aria-hidden="true">{iconPaths[name]}</svg>;
}

export function MoreButton({ label, onClick }: { label: string; onClick: (button: HTMLButtonElement) => void }) {
  return (
    <button
      type="button"
      className="more-button"
      aria-label={label}
      aria-haspopup="menu"
      onClick={(event) => {
        event.stopPropagation();
        onClick(event.currentTarget);
      }}
    >
      <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="5" cy="10" r="1.2" /><circle cx="10" cy="10" r="1.2" /><circle cx="15" cy="10" r="1.2" /></svg>
    </button>
  );
}

const SCROLL_CLOSE_SETTLE_MS = 180;

export function ContextMenu({ menu, onClose }: ContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });

  useLayoutEffect(() => {
    if (!menu) return;
    const element = menuRef.current;
    if (!element) return;
    const margin = 6;
    const rect = element.getBoundingClientRect();
    setPosition({
      left: Math.max(margin, Math.min(menu.x, window.innerWidth - rect.width - margin)),
      top: Math.max(margin, Math.min(menu.placement === "above-start" ? menu.y - rect.height - 4 : menu.y, window.innerHeight - rect.height - margin)),
    });
    const firstEnabled = element.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled), [role="menuitemradio"]:not(:disabled)');
    firstEnabled?.focus();
  }, [menu]);

  useEffect(() => {
    if (!menu) return;
    window.addEventListener("resize", onClose);
    // Opening a menu on a partially visible row scrolls that row into view first
    // (browsers and automation both do this); that scroll must not close the
    // menu it just opened, so the scroll guard arms after a short settle window.
    const armScrollClose = window.setTimeout(() => {
      window.addEventListener("scroll", onClose, true);
    }, SCROLL_CLOSE_SETTLE_MS);
    return () => {
      window.clearTimeout(armScrollClose);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("scroll", onClose, true);
    };
  }, [menu, onClose]);

  if (!menu) return null;

  const closeMenu = (restoreFocus: boolean) => {
    if (restoreFocus) menu.restoreFocus?.focus();
    onClose();
  };

  const moveFocus = (event: KeyboardEvent<HTMLDivElement>, direction: 1 | -1) => {
    const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled), [role="menuitemradio"]:not(:disabled)')];
    if (items.length === 0) return;
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    items[(currentIndex + direction + items.length) % items.length]?.focus();
  };

  return createPortal(
    <div className="context-menu-layer" onPointerDown={() => closeMenu(true)}>
      <div
        ref={menuRef}
        className="context-menu"
        role="menu"
        tabIndex={-1}
        aria-label={menu.label}
        style={{ left: position.left, top: position.top }}
        onPointerDown={(event) => event.stopPropagation()}
        onContextMenu={(event) => event.preventDefault()}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") { event.preventDefault(); moveFocus(event, 1); }
          else if (event.key === "ArrowUp") { event.preventDefault(); moveFocus(event, -1); }
          else if (event.key === "Home" || event.key === "End") {
            event.preventDefault();
            const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled), [role="menuitemradio"]:not(:disabled)')];
            items[event.key === "Home" ? 0 : items.length - 1]?.focus();
          } else if (event.key === "Escape") {
            event.preventDefault();
            closeMenu(true);
          } else if (event.key === "Tab") {
            closeMenu(false);
          }
        }}
      >
        {menu.groupLabel && <div className="context-menu-group-label">{menu.groupLabel}</div>}
        {menu.actions.map((action) => (
          <div key={action.id} className={action.separatorBefore ? "context-menu-entry separated" : "context-menu-entry"}>
            {action.groupLabel && <div className="context-menu-group-label">{action.groupLabel}</div>}
            <button
              type="button"
              role={action.radio ? "menuitemradio" : "menuitem"}
              aria-checked={action.radio ? Boolean(action.checked) : undefined}
              disabled={action.disabled}
              onClick={() => {
                closeMenu(true);
                void action.onSelect();
              }}
            >
              {/* Exactly one element owns the 20px leading column, so an
                  unchecked radio keeps its label in the label column instead of
                  being squeezed into the icon slot. */}
              {action.radio
                ? <span className="context-menu-check">{action.checked ? <MenuIcon name="check" /> : null}</span>
                : <MenuIcon name={action.icon} />}
              <span>{action.label}</span>
            </button>
          </div>
        ))}
      </div>
    </div>,
    document.body,
  );
}
