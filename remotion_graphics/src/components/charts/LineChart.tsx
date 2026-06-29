import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface SeriesPoint {x: number | string; y: number;}
interface Series {name: string; data: SeriesPoint[];}

export const LineChart: React.FC<{
  series: Series[]; title?: string; colors?: string[];
  showMarkers?: boolean; showGrid?: boolean;
  backgroundColor?: string; textColor?: string;
}> = ({
  series, title, colors = ['#7C3AED', '#EC4899', '#06B6D4'],
  showMarkers = true, showGrid = true, backgroundColor = '#0F172A', textColor = '#F8FAFC',
}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const L = 140, R = 1780, T = title ? 180 : 100, B = 920, cw = R - L, ch = B - T;

  const allValues = series.flatMap(s => s.data.map(d => d.y));
  const max = Math.max(...allValues, 1);
  const allX = series[0]?.data.map(d => typeof d.x === 'number' ? d.x : parseFloat(String(d.x))) || [];
  const xMin = Math.min(...allX, 0);
  const xMax = Math.max(...allX, 1);

  const gridLines = [0, 1, 2, 3, 4].map(i => ({value: (max / 4) * i, y: B - (i / 4) * ch}));
  const p = spring({frame, fps, config: {damping: 20}});

  return (
    <AbsoluteFill style={{background: backgroundColor, padding: 40}}>
      <svg viewBox="0 0 1920 1080" style={{width: '100%', height: '100%'}}>
        {title && <text x={960} y={80} textAnchor="middle" fill={textColor} fontFamily="Inter, sans-serif" fontWeight={700} fontSize={48}>{title}</text>}
        {showGrid && gridLines.map((l, i) => (
          <g key={i}>
            <line x1={L} y1={l.y} x2={R} y2={l.y} stroke="#334155" strokeWidth={1} />
            <text x={L - 12} y={l.y + 5} textAnchor="end" fill={textColor} fontFamily="Inter, sans-serif" fontSize={20}>{l.value}</text>
          </g>
        ))}
        <line x1={L} y1={T} x2={L} y2={B} stroke="#475569" strokeWidth={2} />
        <line x1={L} y1={B} x2={R} y2={B} stroke="#475569" strokeWidth={2} />
        {series.map((s, si) => {
          const c = colors[si % colors.length];
          const pts = s.data.map((d, i) => {
            const x = L + ((typeof d.x === 'number' ? d.x : parseFloat(String(d.x))) - xMin) / (xMax - xMin) * cw;
            const y = B - (d.y / max) * ch;
            return `${i === 0 ? 'M' : 'L'}${x},${y}`;
          }).join(' ');
          const drawLen = interpolate(frame, [si * 10, si * 10 + 30], [0, 1], {extrapolateLeft: 'clamp'});
          const fo = interpolate(frame, [durationInFrames - 15, durationInFrames], [1, 0], {extrapolateLeft: 'clamp'});
          return (
            <g key={s.name} opacity={fo}>
              <path d={pts} fill="none" stroke={c} strokeWidth={4} strokeLinecap="round" strokeLinejoin="round"
                strokeDasharray={drawLen < 1 ? '10000' : 'none'}
                strokeDashoffset={drawLen < 1 ? 10000 * (1 - drawLen) : 0}
                opacity={p}
              />
              {showMarkers && s.data.map((d, i) => {
                const x = L + ((typeof d.x === 'number' ? d.x : parseFloat(String(d.x))) - xMin) / (xMax - xMin) * cw;
                const y = B - (d.y / max) * ch;
                const mO = interpolate(frame, [si * 10 + i * 2, si * 10 + i * 2 + 5], [0, 1], {extrapolateLeft: 'clamp'});
                return <circle key={i} cx={x} cy={y} r={6} fill={c} opacity={mO * fo} />;
              })}
            </g>
          );
        })}
        {series.length > 1 && series.map((s, i) => (
          <g key={`leg-${i}`} opacity={spring({frame: frame - 40, fps, config: {damping: 20}})}>
            <rect x={R - 200} y={T - 60 + i * 30} width={16} height={16} rx={3} fill={colors[i % colors.length]} />
            <text x={R - 176} y={T - 47 + i * 30} fill={textColor} fontFamily="Inter, sans-serif" fontSize={18}>{s.name}</text>
          </g>
        ))}
      </svg>
    </AbsoluteFill>
  );
};
