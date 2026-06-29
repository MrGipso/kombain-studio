import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface ComparisonCardProps {
  leftLabel: string;
  rightLabel: string;
  leftValue: string;
  rightValue: string;
  title?: string;
  leftColor?: string;
  rightColor?: string;
  changeIndicator?: string;
  backgroundColor?: string;
  textColor?: string;
}

export const ComparisonCard: React.FC<ComparisonCardProps> = ({
  leftLabel, rightLabel, leftValue, rightValue, title,
  leftColor = '#7C3AED', rightColor = '#10B981',
  changeIndicator, backgroundColor = '#0F172A', textColor = '#F8FAFC',
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const titleO = spring({frame, fps, config: {damping: 20}});
  const leftO = spring({frame: frame - 6, fps, config: {damping: 18}});
  const rightO = spring({frame: frame - 18, fps, config: {damping: 18}});
  const divDraw = spring({frame: frame - 14, fps, config: {damping: 14, stiffness: 80}});
  const indO = spring({frame: frame - 26, fps, config: {damping: 15}});

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: backgroundColor}}>
      <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center', width: '80%', gap: 32}}>
        {title && <div style={{fontFamily: 'Inter, sans-serif', fontWeight: 700, fontSize: 44, color: textColor, opacity: titleO}}>{title}</div>}
        <div style={{display: 'flex', flexDirection: 'row', width: '100%', borderRadius: 16, backgroundColor: '#1E293B', overflow: 'hidden', minHeight: 280}}>
          <div style={{flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', padding: '48px 32px', opacity: leftO, gap: 16}}>
            <div style={{width: 48, height: 4, backgroundColor: leftColor, borderRadius: 2}} />
            <div style={{fontSize: 28, color: textColor, opacity: 0.7, textTransform: 'uppercase', letterSpacing: '0.05em', fontFamily: 'Inter, sans-serif', fontWeight: 600}}>{leftLabel}</div>
            <div style={{fontSize: 72, color: leftColor, fontFamily: 'Inter, sans-serif', fontWeight: 800}}>{leftValue}</div>
          </div>
          <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', width: 80, position: 'relative'}}>
            <div style={{width: 2, height: `${divDraw * 100}%`, backgroundColor: '#475569', position: 'absolute', top: `${(1 - divDraw) / 2 * 100}%`}} />
            {changeIndicator && (
              <div style={{position: 'relative', zIndex: 1, opacity: indO, transform: `scale(${spring({frame: frame - 26, fps, config: {damping: 10, stiffness: 130}, from: 0.6})})`}}>
                <div style={{width: 48, height: 48, borderRadius: 24, background: '#0F172A', display: 'flex', justifyContent: 'center', alignItems: 'center'}}>
                  <span style={{fontSize: 24, color: '#10B981', fontWeight: 700}}>{'\u2191'}</span>
                </div>
                <div style={{fontSize: 18, color: '#10B981', fontWeight: 700, whiteSpace: 'nowrap'}}>{changeIndicator}</div>
              </div>
            )}
          </div>
          <div style={{flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', padding: '48px 32px', opacity: rightO, gap: 16}}>
            <div style={{width: 48, height: 4, backgroundColor: rightColor, borderRadius: 2}} />
            <div style={{fontSize: 28, color: textColor, opacity: 0.7, textTransform: 'uppercase', letterSpacing: '0.05em', fontFamily: 'Inter, sans-serif', fontWeight: 600}}>{rightLabel}</div>
            <div style={{fontSize: 72, color: rightColor, fontFamily: 'Inter, sans-serif', fontWeight: 800}}>{rightValue}</div>
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};
