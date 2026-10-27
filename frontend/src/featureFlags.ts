// Build-time opt-in for the paused materials library; never clears stored data.
export const MATERIALS_ENABLED = import.meta.env.VITE_ENABLE_MATERIALS === "true";
