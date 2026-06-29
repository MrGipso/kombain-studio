import {AbsoluteFill, OffthreadVideo, spring, useCurrentFrame, useVideoConfig} from 'remotion';

export const TitledVideo: React.FC<{
  src: string; title?: string; subtitle?: string;
  overlayColor?: string; backgroundColor?: string;
}> = ({src, title, subtitle, overlayColor = 'rgba(0,0,0,0.5)', backgroundColor = '#0F172A'}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 16}});

  return (
    <AbsoluteFill style={{background: backgroundColor, overflow: 'hidden'}}>
      <OffthreadVideo src={src} style={{width: '100%', height: '100%', objectFit: 'cover'}} muted />
      <AbsoluteFill style={{background: `linear-gradient(to top, ${overlayColor}, transparent 40%, transparent 60%, ${overlayColor})`}} />
      <div style={{position: 'absolute', bottom: 120, left: 0, right: 0, textAlign: 'center', opacity: p, transform: `translateY(${(1 - p) * 20}px)`}}>
        {title && <div style={{fontSize: 52, fontWeight: 800, color: '#FFFFFF', fontFamily: 'Inter, sans-serif', textShadow: '0 4px 20px rgba(0,0,0,0.6)'}}>{title}</div>}
        {subtitle && <div style={{fontSize: 26, color: '#CBD5E1', fontFamily: 'Inter, sans-serif', marginTop: 12, textShadow: '0 2px 10px rgba(0,0,0,0.4)'}}>{subtitle}</div>}
      </div>
      <div style={{position: 'absolute', top: 30, right: 40, fontSize: 14, color: 'rgba(255,255,255,0.5)', fontFamily: 'Inter, sans-serif', letterSpacing: '0.05em', textTransform: 'uppercase'}}>Video</div>
    </AbsoluteFill>
  );
};
