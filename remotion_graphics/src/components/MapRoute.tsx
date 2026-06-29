import React from 'react';
import {interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {baseScene, palette} from '../style';
import type {MotionGraphicProps, Point} from '../types';
import {AnimatedLine, MapBackdrop, MovingDot} from './primitives';

const defaultRoute: Point[] = [
  {x: 1130, y: 470, label: 'Европа'},
  {x: 960, y: 425},
  {x: 760, y: 455},
  {x: 600, y: 500, label: 'США'},
];

export const MapRoute: React.FC<MotionGraphicProps> = ({
  title = 'Данные пересекают океан',
  subtitle = 'Маршрут выглядит цифровым, но проходит через физический кабель',
  route = defaultRoute,
  zones = [],
  accent = palette.accent,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const titleIn = spring({frame, fps, config: {damping: 18, stiffness: 86}});
  const lineStart = Math.round(fps * 0.9);
  const lineEnd = Math.round(fps * 4.2);

  return (
    <div style={baseScene}>
      <MapBackdrop />
      <div style={{position: 'absolute', left: 0, right: 0, top: 76, textAlign: 'center', opacity: titleIn}}>
        <div style={{fontSize: 56, lineHeight: 1.05, fontWeight: 820}}>{title}</div>
        <div style={{fontSize: 28, color: palette.muted, marginTop: 18}}>{subtitle}</div>
      </div>
      <svg width="1920" height="1080" viewBox="0 0 1920 1080" style={{position: 'absolute', inset: 0}}>
        {zones.map((zone, index) => {
          const opacity = interpolate(frame, [fps * 1.5 + index * 8, fps * 2.5 + index * 8], [0, 1], {
            extrapolateLeft: 'clamp',
            extrapolateRight: 'clamp',
          });
          return (
            <g key={zone.label} opacity={opacity}>
              <polygon
                points={zone.points.map((point) => `${point.x},${point.y}`).join(' ')}
                fill={`${zone.color ?? palette.danger}55`}
                stroke={zone.color ?? palette.danger}
                strokeWidth="4"
              />
              <text
                x={zone.points[0]?.x ?? 0}
                y={(zone.points[0]?.y ?? 0) - 22}
                fill={zone.color ?? palette.danger}
                fontSize="26"
                fontWeight="760"
                fontFamily="Inter, Segoe UI, Arial, sans-serif"
              >
                {zone.label}
              </text>
            </g>
          );
        })}
        <AnimatedLine points={route} color={accent} startFrame={lineStart} endFrame={lineEnd} />
        <MovingDot points={route} color={accent} startFrame={lineStart} endFrame={lineEnd} />
        {route
          .filter((point) => point.label)
          .map((point) => (
            <g key={point.label}>
              <circle cx={point.x} cy={point.y} r="16" fill={palette.panel} stroke={accent} strokeWidth="5" />
              <text
                x={point.x}
                y={point.y + 46}
                fill={palette.text}
                fontSize="28"
                fontWeight="760"
                textAnchor="middle"
                fontFamily="Inter, Segoe UI, Arial, sans-serif"
              >
                {point.label}
              </text>
            </g>
          ))}
      </svg>
    </div>
  );
};
