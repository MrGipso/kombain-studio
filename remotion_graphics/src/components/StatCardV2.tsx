import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface StatCardProps {
  stat: string;
  subtitle?: string;
  statFontSize?: number;
  subtitleFontSize?: number;
  color?: string;
  accentColor?: string;
  backgroundColor?: string;
}

export const StatCardV2: React.FC<StatCardProps> = ({
  stat, subtitle, statFontSize = 128, subtitleFontSize = 36,
  color = '#F8FAFC', accentColor = '#F59E0B', backgroundColor = '#1E293B',
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const scale = spring({frame, fps, config: {damping: 12, stiffness: 120}, from: 0.8, to: 1});

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: backgroundColor}}>
      <div style={{textAlign: 'center'}}>
        <div style={{transform: `scale(${scale})`, fontSize: statFontSize, color: accentColor, fontFamily: 'Inter, sans-serif', fontWeight: 800, lineHeight: 1.1}}>
          {stat}
        </div>
        {subtitle && (
          <div style={{opacity: spring({frame: frame - 8, fps, config: {damping: 20}}), fontSize: subtitleFontSize, color, fontFamily: 'Inter, sans-serif', fontWeight: 400, marginTop: 16}}>
            {subtitle}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
