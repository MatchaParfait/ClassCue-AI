import { useQuery } from "@tanstack/react-query";
import { Check, ChevronDown, ChevronRight, Folder, FolderInput, LoaderCircle, RotateCcw } from "lucide-react";
import { useId, useRef, useState, type CSSProperties } from "react";
import { getLibrary, type LibraryFolder, type LibraryResponse } from "../api/client";

interface MoveProps {
  currentFolder: number | null;
  onMove: (folderId: number | null) => void;
}

function childFolders(data: LibraryResponse | undefined, parentId: number | null, ancestors: number[] = []) {
  const seen = new Set(ancestors);
  return (data?.items ?? []).flatMap((item) => {
    const folder = item.folder;
    if (item.type !== "folder" || !folder || (folder.parent_id ?? null) !== parentId || seen.has(folder.id)) return [];
    seen.add(folder.id);
    return [folder];
  });
}

export function VideoMoveMenu({ currentFolder, onMove }: MoveProps) {
  const root = useQuery({
    queryKey: ["library", "root"], queryFn: () => getLibrary(), staleTime: 15_000,
  });
  return <div className="video-move-menu">
    <span className="menu-caption">移动到</span>
    <button role="menuitem" data-move-folder="root" disabled={currentFolder === null}
      title={currentFolder === null ? "当前位于视频库一级目录" : "移动到视频库一级目录"} onClick={() => onMove(null)}>
      <FolderInput size={16} /><span>一级目录</span>{currentFolder === null && <Check className="move-current-icon" size={14} />}
    </button>
    <FolderLoadState query={root} />
    <FolderBranches folders={childFolders(root.data, null)} ancestors={[]} path={[]} currentFolder={currentFolder} onMove={onMove} />
  </div>;
}

function FolderLoadState({ query }: {
  query: { isPending: boolean; isError: boolean; error: unknown; refetch: () => unknown };
}) {
  if (query.isPending) return <div className="move-folder-state" role="status"><LoaderCircle size={14} className="spin" />正在读取文件夹</div>;
  if (query.isError) return <div className="move-folder-state">
    <span role="alert">{query.error instanceof Error ? query.error.message : "文件夹读取失败"}</span>
    <button role="menuitem" data-menu-keep-open title="重新读取文件夹" onClick={() => void query.refetch()}><RotateCcw size={14} />重试</button>
  </div>;
  return null;
}

function FolderBranches({ folders, ancestors, path, ...props }: MoveProps & {
  folders: LibraryFolder[]; ancestors: number[]; path: string[];
}) {
  const counts = new Map<string, number>();
  for (const folder of folders) counts.set(folder.name, (counts.get(folder.name) ?? 0) + 1);
  return <>{folders.map((folder) => <FolderBranch key={folder.id} {...props} folder={folder}
    ancestors={ancestors} path={path} ambiguous={(counts.get(folder.name) ?? 0) > 1} />)}</>;
}

function FolderBranch({ folder, ancestors, path, ambiguous, currentFolder, onMove }: MoveProps & {
  folder: LibraryFolder; ancestors: number[]; path: string[]; ambiguous: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const toggle = useRef<HTMLButtonElement>(null);
  const childrenId = useId();
  const children = useQuery({
    queryKey: ["library", `folder-${folder.id}`], queryFn: () => getLibrary(folder.id),
    enabled: expanded, staleTime: 15_000,
  });
  const ids = [...ancestors, folder.id];
  const names = [...path, folder.name];
  const fullPath = ["视频库", ...names].join(" / ");
  const entries = childFolders(children.data, folder.id, ids);
  const isCurrent = currentFolder === folder.id;
  return <div className="move-folder-branch" data-folder-id={folder.id}>
    <div className={`move-folder-row${isCurrent ? " is-current" : ""}`}
      style={{ "--folder-indent": `${Math.min(ancestors.length, 6) * 16}px` } as CSSProperties}
      onKeyDown={(event) => {
        if (event.key === "ArrowRight") { event.preventDefault(); event.stopPropagation(); setExpanded(true); toggle.current?.focus(); }
        if (event.key === "ArrowLeft" && expanded) { event.preventDefault(); event.stopPropagation(); setExpanded(false); toggle.current?.focus(); }
      }}>
      <button className="move-folder-toggle" ref={toggle} role="menuitem" data-menu-keep-open
        aria-label={`${expanded ? "收起" : "展开"}文件夹「${fullPath}」`} aria-expanded={expanded}
        aria-controls={expanded ? childrenId : undefined} title={`${expanded ? "收起" : "展开"}子文件夹`}
        onClick={() => setExpanded((value) => !value)}>
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      <button className="move-folder-choice" role="menuitem" data-move-folder={folder.id}
        disabled={isCurrent} title={`${fullPath}${ambiguous ? `（文件夹 #${folder.id}）` : ""}${isCurrent ? "（当前位置）" : ""}`}
        onClick={() => onMove(folder.id)}>
        <Folder size={16} /><span>{folder.name}</span>
        {ambiguous && <small className="move-folder-id">#{folder.id}</small>}
        {isCurrent && <Check className="move-current-icon" size={14} />}
      </button>
    </div>
    {expanded && <div className="move-folder-children" id={childrenId} role="group" aria-label={`${fullPath}的子文件夹`}>
      <FolderLoadState query={children} />
      {!children.isPending && !children.isError && entries.length === 0 && <div className="move-folder-state">无子文件夹</div>}
      <FolderBranches folders={entries} ancestors={ids} path={names} currentFolder={currentFolder} onMove={onMove} />
    </div>}
  </div>;
}
