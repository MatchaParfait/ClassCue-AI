const ART = "/art/settings-herons";
const HERONS = [
  { side: "left", width: 445, head: 453, body: 1042 },
  { side: "right", width: 511, head: 461, body: 1029 },
] as const;

export function SettingsHerons() {
  return <div className="settings-herons" aria-hidden="true">
    {HERONS.map(({ side, width, head, body }) => <div key={side} className={`settings-heron settings-heron-${side}`}>
      <img className="settings-heron-head" src={`${ART}/${side}-head.webp`} width={width} height={head} alt="" draggable={false} decoding="async" />
      <img className="settings-heron-neck" src={`${ART}/${side}-neck.webp`} width={width} height={1} alt="" draggable={false} decoding="async" />
      <img className="settings-heron-body" src={`${ART}/${side}-body.webp`} width={width} height={body} alt="" draggable={false} decoding="async" />
    </div>)}
  </div>;
}
