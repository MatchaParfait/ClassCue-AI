import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Cable } from "lucide-react";
import { openDeviceTransfer, type TransferSelection } from "../api/deviceTransfers";

const TransferDialog = lazy(() => import("./DeviceTransferDialog"));

export function DeviceTransferButton({ active, libraryId, selectedVideoIds = [] }: {
  active: boolean; libraryId?: string; selectedVideoIds?: number[];
}) {
  const [open, setOpen] = useState(false);
  const [selection, setSelection] = useState<TransferSelection>();
  const trigger = useRef<HTMLButtonElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!active || !libraryId) return;
    const show = (event: Event) => {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : trigger.current;
      setSelection((event as CustomEvent<TransferSelection | undefined>).detail);
      setOpen(true);
    };
    window.addEventListener("classcue:device-transfer", show);
    return () => window.removeEventListener("classcue:device-transfer", show);
  }, [active, libraryId]);
  return <>
    <button ref={trigger} type="button" className="text-button" disabled={!active || !libraryId}
      onClick={() => {
        opener.current = trigger.current;
        setSelection(selectedVideoIds.length ? { title: `已选 ${selectedVideoIds.length} 个视频`, videoIds: selectedVideoIds } : undefined);
        setOpen(true);
      }}><Cable size={16} />连接平板</button>
    {open && active && libraryId && <Suspense fallback={<span role="status">正在打开设备连接…</span>}>
      <TransferDialog key={libraryId} libraryId={libraryId} selection={selection}
        returnFocus={opener.current} onClose={() => {
          setOpen(false);
          window.requestAnimationFrame(() => (opener.current?.isConnected ? opener.current : trigger.current)?.focus());
        }} />
    </Suspense>}
  </>;
}

export function DeviceTransferSendMenuItem({ selection, disabled }: { selection: TransferSelection; disabled?: boolean }) {
  return <button type="button" role="menuitem" disabled={disabled} onClick={() => openDeviceTransfer(selection)}>
    <Cable size={16} />发送到平板
  </button>;
}
