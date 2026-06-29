import {AbsoluteFill, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface CalloutBoxProps {
  text: string;
  type?: 'info' | 'warning' | 'tip' | 'quote';
  title?: string;
  borderColor?: string;
  textColor?: string;
  backgroundColor?: string;
}

const TYPE_STYLES: Record<string, {icon: string; border: string}> = {
  info: {icon: '\u2139\uFE0F', border: '#3B82F6'},
  warning: {icon: '\u26A0\uFE0F', border: '#F59E0B'},
  tip: {icon: '\uD83D\uDCA1', border: '#10B981'},
  quote: {icon: '\u275D', border: '#8B5CF6'},
};

export const CalloutBox: React.FC<CalloutBoxProps> = ({
  text, type = 'info', title, borderColor, textColor = '#F8FAFC', backgroundColor = '#1E293B',
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const opacity = spring({frame, fps, config: {damping: 18}});
  const slideUp = spring({frame, fps, config: {damping: 14}, from: 20, to: 0});
  const style = TYPE_STYLES[type];
  const border = borderColor || style.border;

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', padding: 80}}>
      <div style={{
        opacity, transform: `translateY(${slideUp}px)`,
        background: backgroundColor, borderRadius: 16, padding: '40px 48px', maxWidth: '80%',
        borderLeft: `6px solid ${border}`, boxShadow: '0 4px 24px rgba(0,0,0,0.2)',
      }}>
        <div style={{fontSize: 32, marginBottom: 12}}>{style.icon} {title && <span style={{fontWeight: 700, marginLeft: 8}}>{title}</span>}</div>
        <div style={{fontSize: 28, color: textColor, fontFamily: 'Inter, sans-serif', lineHeight: 1.5, fontWeight: type === 'quote' ? 400 : 500, fontStyle: type === 'quote' ? 'italic' : 'normal'}}>
          {text}
        </div>
      </div>
    </AbsoluteFill>
  );
};
