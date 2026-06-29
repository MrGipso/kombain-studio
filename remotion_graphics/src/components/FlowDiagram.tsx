import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {baseScene, palette} from '../style';
import type {MotionGraphicProps} from '../types';

export const FlowDiagram: React.FC<MotionGraphicProps> = ({
  title = '5G не отменяет кабели',
  subtitle = 'Беспроводным остается только последний участок до вышки',
  nodes = ['Телефон', '5G-вышка', 'Оптоволокно', 'Дата-центр', 'Подводный кабель'],
  accent = palette.accent,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const titleIn = spring({frame, fps, config: {damping: 20, stiffness: 90}});
  const y = 610;
  const left = 190;
  const right = 1730;
  const gap = nodes.length > 1 ? (right - left) / (nodes.length - 1) : 0;

  return (
    <div style={baseScene}>
      <div style={{position: 'absolute', left: 0, right: 0, top: 74, textAlign: 'center', opacity: titleIn}}>
        <div style={{fontSize: 56, lineHeight: 1.05, fontWeight: 800}}>{title}</div>
        <div style={{fontSize: 28, color: palette.muted, marginTop: 18}}>{subtitle}</div>
      </div>
      {nodes.slice(0, -1).map((_, index) => {
        const start = fps * 0.7 + index * 10;
        const progress = interpolate(frame, [start, start + 18], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
        const x1 = left + index * gap + 92;
        const x2 = left + (index + 1) * gap - 92;
        return (
          <div
            key={`line-${index}`}
            style={{
              position: 'absolute',
              left: x1,
              top: y - 3,
              width: (x2 - x1) * progress,
              height: 6,
              borderRadius: 999,
              background: accent,
              boxShadow: `0 0 24px ${accent}88`,
            }}
          />
        );
      })}
      {nodes.map((node, index) => {
        const start = fps * 0.35 + index * 10;
        const appear = spring({frame: frame - start, fps, config: {damping: 16, stiffness: 100}});
        const x = left + index * gap;
        return (
          <div
            key={node}
            style={{
              position: 'absolute',
              left: x - 86,
              top: y - 86,
              width: 172,
              height: 172,
              borderRadius: '50%',
              background: palette.panel,
              border: `4px solid ${accent}`,
              boxShadow: '0 18px 54px rgba(0,0,0,0.34)',
              transform: `scale(${0.72 + appear * 0.28})`,
              opacity: appear,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              textAlign: 'center',
              padding: 20,
              fontSize: 31,
              lineHeight: 1.08,
              fontWeight: 760,
            }}
          >
            {node}
          </div>
        );
      })}
    </div>
  );
};
