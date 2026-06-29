import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';

interface TerminalLine {
  text: string; color?: string; delay?: number; typing?: boolean;
}

export const TerminalScene: React.FC<{
  lines: TerminalLine[]; title?: string;
  backgroundColor?: string; prompt?: string;
}> = ({lines, title, backgroundColor = '#0D1117', prompt = '$'}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();

  const allText = lines.reduce((s, l) => s + l.text.length + 1, 0);
  const cursor = Math.min(frame * 2.5, allText);

  return (
    <AbsoluteFill style={{background: backgroundColor, padding: 80, display: 'flex', justifyContent: 'center'}}>
      {title && <div style={{fontSize: 20, color: '#8B949E', fontFamily: 'Fira Code, monospace', marginBottom: 24, opacity: spring({frame, fps, config: {damping: 20}})}}>{title}</div>}
      <div style={{
        background: '#161B22', borderRadius: 12, padding: 32, border: '1px solid #30363D',
        fontFamily: 'Fira Code, monospace', fontSize: 22, lineHeight: 1.6, maxWidth: 1600,
      }}>
        <div style={{display: 'flex', gap: 8, marginBottom: 20}}>
          {['#FF5F56', '#FFBD2E', '#27C93F'].map((c, i) => <div key={i} style={{width: 14, height: 14, borderRadius: '50%', background: c}} />)}
        </div>
        {lines.map((line, i) => {
          const lineStart = lines.slice(0, i).reduce((s, l) => s + l.text.length + 1, 0);
          const lineEnd = lineStart + line.text.length;
          const visible = cursor >= lineStart ? Math.min(cursor - lineStart, line.text.length) : 0;
          const display = line.typing !== false ? line.text.slice(0, visible) : line.text;
          const done = visible >= line.text.length;
          const fade = spring({frame: frame - i * 3 - 10, fps, config: {damping: 20}});
          return (
            <div key={i} style={{color: line.color || '#E6EDF3', opacity: fade, minHeight: 36}}>
              <span style={{color: '#8B949E'}}>{prompt} </span>
              {display}
              {!done && <span style={{color: '#58A6FF', fontWeight: 700}}>▍</span>}
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
