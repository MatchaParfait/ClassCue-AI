// Build-time opt-in for the paused materials library; never clears stored data.
export const MATERIALS_ENABLED = import.meta.env.VITE_ENABLE_MATERIALS === "true";
// Keep the unfinished Bilibili study UI hidden until explicitly enabled for development.
export const BILIBILI_STUDY_ENABLED = import.meta.env.VITE_ENABLE_BILIBILI_STUDY === "true";
