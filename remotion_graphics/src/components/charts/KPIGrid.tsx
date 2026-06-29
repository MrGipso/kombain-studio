import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface KPIMetric {label: string; value: string; change?: string;}

export const KPIGrid: React.FC<{
  metrics: KPIMetric[]; title?: string; columns?: 2 | 3 | 4;
  colors?: string[]; backgroundColor?: string;
}> = ({
  metrics, title, columns = 3, colors = ['#7C3AED', '#EC4899', '#06B6D4', '#F59E0B', '#10B981'],
  backgroundColor = '#0F172A',
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const cols = columns || 3;
  const rows = Math.ceil(metrics.length / cols);

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: backgroundColor, padding: 60}}>
      {title && <div style={{fontSize: 44, fontWeight: 700, color: '#F8FAFC', fontFamily: 'Inter, sans-serif', marginBottom: 40, opacity: spring({frame, fps, config: {damping: 20}})}}>{title}</div>}
      <div style={{display: 'grid', gridTemplateColumns: `repeat(${cols}, 1fr)`, gap: 16, width: '100%', maxWidth: 1600}}>
        {metrics.map((m, i) => {
          const row = Math.floor(i / cols);
          const d = spring({frame: frame - i * 2, fps, config: {damping: 14}});
          return (
            <div key={i} style={{
              opacity: d, transform: `translateY(${(1 - d) * 20}px)`,
              background: '#1E293B', borderRadius: 12, padding: '28px 24px',
              borderTop: `4px solid ${colors[i % colors.length]}`,
            }}>
              <div style={{fontSize: 14, color: '#94A3B8', fontFamily: 'Inter, sans-serif', fontWeight: 500, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 8}}>{m.label}</div>
              <div style={{fontSize: 40, color: '#F8FAFC', fontFamily: 'Inter, sans-serif', fontWeight: 800}}>{m.value}</div>
              {m.change && <div style={{fontSize: 16, color: m.change.startsWith('+') ? '#10B981' : '#EF4444', fontFamily: 'Inter, sans-serif', fontWeight: 600, marginTop: 4}}>{m.change}</div>}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
