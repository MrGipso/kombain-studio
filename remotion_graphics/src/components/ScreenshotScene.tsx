import {AbsoluteFill, Img, spring, useCurrentFrame, useVideoConfig} from 'remotion';

export const ScreenshotScene: React.FC<{
  image: string; title?: string; subtitle?: string;
  device?: 'phone' | 'tablet' | 'browser'; backgroundColor?: string;
}> = ({image, title, subtitle, device = 'browser', backgroundColor = '#0F172A'}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const p = spring({frame, fps, config: {damping: 16}});

  const dims = device === 'phone' ? {w: 360, h: 720, r: 40}
    : device === 'tablet' ? {w: 600, h: 800, r: 32}
    : {w: 1100, h: 680, r: 8};

  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: backgroundColor, padding: 60}}>
      {title && <div style={{fontSize: 40, fontWeight: 700, color: '#F8FAFC', fontFamily: 'Inter, sans-serif', marginBottom: subtitle ? 8 : 30, opacity: p}}>{title}</div>}
      {subtitle && <div style={{fontSize: 22, color: '#94A3B8', fontFamily: 'Inter, sans-serif', marginBottom: 30, opacity: p}}>{subtitle}</div>}
      <div style={{
        opacity: p, transform: `translateY(${(1 - p) * 30}px) scale(${p})`,
        background: device === 'browser' ? '#1E293B' : '#0F172A',
        borderRadius: dims.r, padding: device === 'browser' ? '40px 0 0 0' : 8,
        boxShadow: '0 30px 60px rgba(0,0,0,0.5)',
        width: dims.w, height: device === 'browser' ? dims.h : dims.h + 16,
        overflow: 'hidden', display: 'flex', flexDirection: 'column',
      }}>
        {device === 'browser' && (
          <div style={{display: 'flex', gap: 8, padding: '12px 16px', alignItems: 'center'}}>
            {['#FF5F56', '#FFBD2E', '#27C93F'].map((c, i) => <div key={i} style={{width: 12, height: 12, borderRadius: '50%', background: c}} />)}
            <div style={{background: '#0F172A', borderRadius: 6, padding: '4px 12px', marginLeft: 16, fontSize: 12, color: '#64748B', flex: 1, fontFamily: 'Inter, sans-serif'}}>{title || 'https://example.com'}</div>
          </div>
        )}
        <Img src={image} style={{width: '100%', height: '100%', objectFit: 'cover', borderRadius: device === 'browser' ? 0 : 4}} />
      </div>
    </AbsoluteFill>
  );
};
