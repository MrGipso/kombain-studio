import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface ProgressBarProps {
  progress: number;
  label?: string;
  color?: string;
  backgroundColor?: string;
  segments?: {label: string; value: number; color?: string}[];
}

export const ProgressBar: React.FC<ProgressBarProps> = ({
  progress, label, color = '#7C3AED', backgroundColor = '#1E293B', segments,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 14, stiffness: 80}, from: 0, to: progress / 100});
  const labelO = spring({frame: frame - 10, fps, config: {damping: 20}});

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: backgroundColor, padding: '80px 120px'}}>
      <div style={{width: '100%', maxWidth: 1200}}>
        {label && (
          <div style={{fontSize: 32, fontWeight: 700, color: '#F8FAFC', fontFamily: 'Inter, sans-serif', marginBottom: 24, opacity: labelO}}>
            {label}
          </div>
        )}
        {segments ? (
          <div style={{display: 'flex', height: 40, borderRadius: 20, overflow: 'hidden', background: '#334155'}}>
            {segments.map((seg, i) => {
              const segP = spring({frame: frame - i * 3, fps, config: {damping: 14}, from: 0, to: seg.value / 100});
              return <div key={i} style={{width: `${segP * 100}%`, background: seg.color || color, transition: 'none'}} />;
            })}
          </div>
        ) : (
          <div style={{height: 40, borderRadius: 20, background: '#334155', overflow: 'hidden'}}>
            <div style={{width: `${p * 100}%`, height: '100%', background: color, borderRadius: 20, transition: 'none'}} />
          </div>
        )}
        <div style={{display: 'flex', justifyContent: 'space-between', marginTop: 12}}>
          <span style={{fontSize: 20, color: '#94A3B8', fontFamily: 'Inter, sans-serif'}}>0%</span>
          <span style={{fontSize: 28, fontWeight: 700, color, fontFamily: 'Inter, sans-serif', opacity: interpolate(p, [0.5, 1], [0, 1])}}>{Math.round(p * 100)}%</span>
          <span style={{fontSize: 20, color: '#94A3B8', fontFamily: 'Inter, sans-serif'}}>100%</span>
        </div>
      </div>
    </AbsoluteFill>
  );
};
