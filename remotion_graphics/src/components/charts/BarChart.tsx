import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface BarDatum {label: string; value: number;}

export const BarChart: React.FC<{
  data: BarDatum[]; title?: string; colors?: string[];
  showValues?: boolean; showGrid?: boolean;
  backgroundColor?: string; textColor?: string;
}> = ({
  data, title,
  colors = ['#7C3AED', '#EC4899', '#06B6D4', '#F59E0B', '#10B981', '#EF4444'],
  showValues = true, showGrid = true, backgroundColor = '#0F172A', textColor = '#F8FAFC',
}) => {
  const frame = useCurrentFrame();
  const {fps, durationInFrames} = useVideoConfig();
  const max = Math.max(...data.map(d => d.value), 1);
  const L = 140, R = 1780, T = title ? 160 : 80, B = 920, cw = R - L, ch = B - T;
  const gap = data.length > 6 ? 8 : 12;
  const bw = Math.min((cw - gap * (data.length + 1)) / data.length, 120);
  const tw = data.length * bw + (data.length + 1) * gap;
  const ox = L + (cw - tw) / 2;

  const gridLines = [0, 1, 2, 3, 4, 5].map(i => ({value: (max / 5) * i, y: B - (i / 5) * ch}));

  return (
    <AbsoluteFill style={{background: backgroundColor, padding: 40}}>
      <svg viewBox="0 0 1920 1080" style={{width: '100%', height: '100%'}}>
        {title && <text x={960} y={80} textAnchor="middle" fill={textColor} fontFamily="Inter, sans-serif" fontWeight={700} fontSize={48} opacity={spring({frame, fps, config: {damping: 20}})}>{title}</text>}
        {showGrid && gridLines.map((l, i) => (
          <g key={i}>
            <line x1={L} y1={l.y} x2={R} y2={l.y} stroke="#334155" strokeWidth={1} opacity={interpolate(frame, [0, 10], [0, 0.6], {extrapolateRight: 'clamp'})} />
            <text x={L - 12} y={l.y + 5} textAnchor="end" fill={textColor} fontFamily="Inter, sans-serif" fontSize={20} opacity={interpolate(frame, [0, 10], [0, 0.6], {extrapolateRight: 'clamp'})}>{l.value >= 1000 ? `${(l.value / 1000).toFixed(1)}K` : l.value}</text>
          </g>
        ))}
        <line x1={L} y1={T} x2={L} y2={B} stroke="#334155" strokeWidth={2} opacity={interpolate(frame, [0, 8], [0, 1], {extrapolateRight: 'clamp'})} />
        <line x1={L} y1={B} x2={R} y2={B} stroke="#334155" strokeWidth={2} opacity={interpolate(frame, [0, 8], [0, 1], {extrapolateRight: 'clamp'})} />
        {data.map((d, i) => {
          const c = colors[i % colors.length];
          const bx = ox + gap + i * (bw + gap);
          const bh = (d.value / max) * ch;
          const p = spring({frame: frame - i * 4, fps, config: {damping: 14, stiffness: 80}});
          const fO = interpolate(frame, [durationInFrames - 15, durationInFrames], [1, 0], {extrapolateLeft: 'clamp'});
          return (
            <g key={d.label} opacity={fO}>
              <rect x={bx} y={B - bh * p} width={bw} height={Math.max(bh * p, 0)} fill={c} rx={4} opacity={interpolate(frame, [i * 4, i * 4 + 6], [0, 1], {extrapolateLeft: 'clamp'})} />
              {showValues && <text x={bx + bw / 2} y={B - bh * p - 12} textAnchor="middle" fill={textColor} fontFamily="Inter, sans-serif" fontWeight={600} fontSize={22} opacity={interpolate(p, [0.7, 1], [0, 1], {extrapolateLeft: 'clamp'})}>{d.value}</text>}
              <text x={bx + bw / 2} y={B + 40} textAnchor="middle" fill={textColor} fontFamily="Inter, sans-serif" fontSize={20} opacity={interpolate(frame, [i * 4, i * 4 + 6], [0, 1], {extrapolateLeft: 'clamp'})}>{d.label}</text>
            </g>
          );
        })}
      </svg>
    </AbsoluteFill>
  );
};
