/* 마이크 입력을 16bit PCM 으로 바꿔 메인 스레드로 넘기는 AudioWorklet.
 *
 * 브라우저의 MediaRecorder 를 쓰지 않는 이유가 있다. MediaRecorder 가 만드는 webm/opus
 * 는 첫 조각에만 컨테이너 헤더가 있어서, 중간 조각을 떼어 서버로 보내면 디코딩되지 않는다.
 * 원시 PCM 을 보내면 서버가 어느 지점에서든 온전한 WAV 를 만들 수 있다.
 *
 * 이 파일은 AudioWorklet 컨텍스트에서 돈다. window/document 가 없고, CSP 상 같은 출처의
 * 모듈이어야 한다 (script-src 'self').
 */

// 128 프레임마다 보내면 메시지가 너무 잦다. 약 0.25초 분량씩 모아 보낸다.
const FRAMES_PER_MESSAGE = 4096;

class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Int16Array(FRAMES_PER_MESSAGE);
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    // 입력이 아직 없거나 트랙이 끝난 상태다. 프로세서는 계속 살려 둔다.
    if (!channel) return true;

    for (let i = 0; i < channel.length; i += 1) {
      // Float32(-1.0~1.0) 를 16bit 정수로 옮긴다. 범위를 넘는 값은 잘라 낸다 —
      // 그대로 두면 정수 오버플로로 반대 부호가 되어 심한 잡음이 된다.
      let sample = channel[i];
      if (sample > 1) sample = 1;
      else if (sample < -1) sample = -1;
      this.buffer[this.offset] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this.offset += 1;

      if (this.offset === this.buffer.length) {
        // 복사본을 넘긴다. 버퍼를 그대로 전송하면 다음 프레임에서 덮어쓰게 된다.
        this.port.postMessage(this.buffer.slice(0));
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
