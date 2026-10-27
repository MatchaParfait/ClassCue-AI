import { ArrowLeft, BookOpen } from "lucide-react";
import type { RefObject } from "react";
import { Link } from "react-router-dom";
import type { Video } from "../types";
import { CharacterEye } from "./CharacterAccent";
import { GlobalSearchButton } from "./GlobalSearch";
import { ReviewTabs } from "./ReviewTabs";
import { Announcements } from "./Announcements";
import { ReviewTaskCenter } from "./TaskCenter";

interface TopBarProps {
  video?: Video;
  demo?: boolean;
  backgroundRef?: RefObject<HTMLDivElement>;
}

export function TopBar({ video, demo, backgroundRef }: TopBarProps) {
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
        <ReviewTaskCenter demo={demo} backgroundRef={backgroundRef} />
      </div>
      <ReviewTabs />
    </header>
  );
}
