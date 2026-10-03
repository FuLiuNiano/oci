import React from "react";
import "../static/manga-layout.css";

/** CSS Grid layout: columns may be 2 or 3, or omitted for mixed panel sizes. */
export function MangaLayout({ children, className = "", ...props }) {
  return <div className={`manga-layout ${className}`} {...props}>{children}</div>;
}

export function MangaGrid({ children, columns, className = "", ...props }) {
  return <div className={`manga-grid ${className}`} data-columns={columns === 2 || columns === 3 ? columns : undefined} {...props}>{children}</div>;
}

/** A panel accepts text/JSX children and an optional image with meaningful alt text. */
export function MangaPanel({ children, caption, image, span = "regular", tall = false, stable = false, className = "", ...props }) {
  const size = ["regular", "wide", "feature", "full"].includes(span) ? span : "regular";
  const classes = ["manga-panel", `manga-panel--${size}`, tall && "manga-panel--tall", stable && "manga-panel--stable", className].filter(Boolean).join(" ");
  return (
    <article className={classes} {...props}>
      {caption && <div className="manga-caption">{caption}</div>}
      {image && <img className="manga-image" src={image.src} alt={image.alt} width={image.width} height={image.height} loading="lazy" />}
      {children}
    </article>
  );
}

export function SpeechBubble({ children, speaker, tail = "left", tone = "normal", className = "", ...props }) {
  const classes = ["speech-bubble", tail === "right" && "speech-bubble--right", tone === "warning" && "speech-bubble--warning", className].filter(Boolean).join(" ");
  return <div className={classes} {...props}>{speaker && <strong className="speech-speaker">{speaker}</strong>}{children}</div>;
}

/** Example: a full-width key scene followed by two- and three-column dialogue. */
export default function MangaPageExample() {
  return (
    <MangaLayout>
      <MangaGrid>
        <MangaPanel span="full" caption="Chapter 01 / 云端工作台" stable>
          <h1>每一次连接，都是新的篇章。</h1>
          <p>全宽主分镜可以放关键操作、终端或概览。</p>
        </MangaPanel>
        <MangaPanel span="feature" caption="Scene 01" image={{src: "/static/assets/manga-hero.svg", alt: "云朵与闪电的原创漫画插画", width: 800, height: 400}}>
          <h2>画面和文字，同框登场。</h2>
        </MangaPanel>
        <MangaPanel caption="Scene 02">
          <SpeechBubble speaker="管理员">会话已经准备好了。</SpeechBubble>
          <SpeechBubble speaker="系统" tail="right">开始你的下一步操作吧。</SpeechBubble>
        </MangaPanel>
      </MangaGrid>
      <h2>对话分镜</h2>
      <MangaGrid columns={3}>
        <MangaPanel caption="01"><SpeechBubble speaker="连接">支持图片与任意 JSX 内容。</SpeechBubble></MangaPanel>
        <MangaPanel caption="02"><SpeechBubble speaker="操作" tail="right">悬停时轻微旋转。</SpeechBubble></MangaPanel>
        <MangaPanel caption="03"><SpeechBubble speaker="提醒" tone="warning">小屏幕自动变成单列。</SpeechBubble></MangaPanel>
      </MangaGrid>
    </MangaLayout>
  );
}
