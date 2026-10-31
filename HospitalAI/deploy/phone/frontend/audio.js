/** Serializes audio generation results without treating synthesis as playback. */
export class AudioQueue {
  constructor(player, panel, label, help) {
    this.player = player;
    this.panel = panel;
    this.label = label;
    this.help = help;
    this.queue = [];
    this.current = null;
    this.player.addEventListener("ended", () => this.next());
  }

  enqueue(blob, label) {
    this.queue.push({ url: URL.createObjectURL(blob), label });
    if (!this.current) this.next();
  }

  next() {
    if (this.current) URL.revokeObjectURL(this.current.url);
    this.current = this.queue.shift() || null;
    if (!this.current) return;
    this.panel.classList.remove("hidden");
    this.label.textContent = this.current.label;
    this.player.src = this.current.url;
    this.player.play().catch(() => {
      this.help.textContent = "자동 재생이 차단되었습니다. 재생 버튼을 눌러 주세요.";
    });
  }

  clear() {
    this.player.pause();
    if (this.current) URL.revokeObjectURL(this.current.url);
    for (const item of this.queue) URL.revokeObjectURL(item.url);
    this.current = null;
    this.queue = [];
    this.player.removeAttribute("src");
    this.player.load();
    this.panel.classList.add("hidden");
    this.help.textContent = "음성이 자동 재생되지 않으면 재생 버튼을 눌러 주세요.";
  }
}
