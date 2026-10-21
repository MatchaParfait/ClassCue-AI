import { ArrowLeft, BookOpen, Settings2 } from "lucide-react";
import { Link } from "react-router-dom";
import type { Video } from "../types";
import { CharacterEye } from "./CharacterAccent";
import { GlobalSearchButton } from "./GlobalSearch";
import { ReviewTabs } from "./ReviewTabs";
import { Announcements } from "./Announcements";

interface TopBarProps {
  video?: Video;
  onOpenSettings?: () => void;
}

export function TopBar({ video, onOpenSettings }: TopBarProps) {
  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Link className="back-link" to="/">
          <ArrowLeft size={17} aria-hidden="true" />
          <span>返回视频库</span>
        </Link>
        <div className="brand-mark">
          <BookOpen size={19} aria-hidden="true" />
          <CharacterEye />
          <span>课堂重点复习</span>
        </div>
        <div className="topbar-search-tools"><div className="topbar-video">
          <span title={video?.display_name}>课程复习</span>
        </div><GlobalSearchButton /><Announcements /></div>
        <button className="icon-button settings-button" type="button" disabled={!onOpenSettings} onClick={onOpenSettings} title="打开复习设置" aria-label="打开复习设置">
          <Settings2 size={18} aria-hidden="true" />
          <span>复习设置</span>
        </button>
      </div>
      <ReviewTabs />
    </header>
  );
}
