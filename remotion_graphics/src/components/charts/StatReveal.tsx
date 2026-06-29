import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface StatRevealDatum {label: string; value: number; suffix?: string; prefix?: string; decimals?: number;}

export const StatReveal: React.FC<{
  data: StatRevealDatum[]; title?: string; columns?: 2 | 3 | 4;
  accentColor?: string; backgroundColor?: string;
}> = ({data, title, columns = 3, accentColor = '#7C3AED', backgroundColor = '#0F172A'}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: backgroundColor, padding: 60}}>
      {title && <div style={{fontSize: 44, fontWeight: 700, color: '#F8FAFC', fontFamily: 'Inter, sans-serif', marginBottom: 50, opacity: spring({frame, fps, config: {damping: 20}})}}>{title}</div>}
      <div style={{display: 'grid', gridTemplateColumns: `repeat(${columns}, 1fr)`, gap: 24, width: '100%', maxWidth: 1600, textAlign: 'center'}}>
        {data.map((d, i) => {
          const appear = spring({frame: frame - i * 4, fps, config: {damping: 14}});
          const count = interpolate(appear, [0, 1], [0, d.value]);
          const display = d.prefix ?? '' + count.toFixed(d.decimals ?? 0) + (d.suffix ?? '');
          return (
            <div key={i} style={{opacity: appear, transform: `translateY(${(1 - appear) * 30}px)`}}>
              <div style={{fontSize: 64, fontWeight: 800, color: accentColor, fontFamily: 'Inter, sans-serif', lineHeight: 1}}>{display}</div>
              <div style={{fontSize: 18, color: '#94A3B8', fontFamily: 'Inter, sans-serif', fontWeight: 500, marginTop: 8}}>{d.label}</div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
