import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface PieDatum {label: string; value: number;}

export const PieChart: React.FC<{
  data: PieDatum[]; title?: string; colors?: string[];
  donut?: boolean; centerLabel?: string; centerValue?: string;
  showLegend?: boolean; backgroundColor?: string;
}> = ({
  data, title, colors = ['#7C3AED', '#EC4899', '#06B6D4', '#F59E0B', '#10B981', '#EF4444'],
  donut, centerLabel, centerValue, showLegend = true, backgroundColor = '#0F172A',
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const total = data.reduce((s, d) => s + d.value, 0) || 1;
  const cx = 960, cy = 540, r = 280, ir = donut ? 140 : 0;

  let cum = 0;
  const slices = data.map((d, i) => {
    const angle = (d.value / total) * 360;
    const start = cum;
    cum += angle;
    const p = spring({frame: frame - i * 3, fps, config: {damping: 14}});
    return {...d, start, angle: angle * p, color: colors[i % colors.length]};
  });

  const polarToCart = (deg: number) => {
    const rad = ((deg - 90) * Math.PI) / 180;
    return {x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad)};
  };

  return (
    <AbsoluteFill style={{background: backgroundColor, padding: 40}}>
      <svg viewBox="0 0 1920 1080" style={{width: '100%', height: '100%'}}>
        {title && <text x={960} y={80} textAnchor="middle" fill="#F8FAFC" fontFamily="Inter, sans-serif" fontWeight={700} fontSize={48} opacity={spring({frame, fps, config: {damping: 20}})}>{title}</text>}
        {slices.map((s, i) => {
          if (s.angle <= 0) return null;
          const p1 = polarToCart(s.start);
          const p2 = polarToCart(s.start + Math.min(s.angle, 359.9));
          const large = s.angle > 180 ? 1 : 0;
          const d = `M ${cx},${cy} L ${p1.x},${p1.y} A ${r},${r} 0 ${large} 1 ${p2.x},${p2.y} Z`;
          return <path key={i} d={d} fill={s.color} opacity={interpolate(s.angle, [0, s.angle], [0, 1])} stroke={backgroundColor} strokeWidth={2} />;
        })}
        {donut && <circle cx={cx} cy={cy} r={ir} fill={backgroundColor} />}
        {centerValue && <text x={cx} y={cy - 10} textAnchor="middle" fill="#F8FAFC" fontFamily="Inter, sans-serif" fontWeight={800} fontSize={48}>{centerValue}</text>}
        {centerLabel && <text x={cx} y={cy + 30} textAnchor="middle" fill="#94A3B8" fontFamily="Inter, sans-serif" fontSize={24}>{centerLabel}</text>}
        {showLegend && slices.map((s, i) => (
          <g key={i} opacity={spring({frame: frame - 20 - i * 2, fps, config: {damping: 18}})}>
            <rect x={1400} y={200 + i * 40} width={20} height={20} rx={4} fill={s.color} />
            <text x={1430} y={216 + i * 40} fill="#F8FAFC" fontFamily="Inter, sans-serif" fontSize={20}>{s.label}</text>
            <text x={1750} y={216 + i * 40} textAnchor="end" fill="#94A3B8" fontFamily="Inter, sans-serif" fontSize={18}>{Math.round(s.value / total * 100)}%</text>
          </g>
        ))}
      </svg>
    </AbsoluteFill>
  );
};
