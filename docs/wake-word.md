# Choosing a wake word

echo-dot-assist offers two wake-word engines. You pick one on the Echo's settings page (`http://<echo-ip>:28931/`, **Wake word**).

## Amazon's engine (recommended)

This is the Echo's original detector, tuned for its microphone array. It wakes reliably from across the room.

- **Alexa** is built in.
- **Computer**, **Echo** and **Ziggy** can be added with **Download from Amazon**. That step asks for an Amazon sign-in code, once.

## microWakeWord

Open models that run on the Echo. In practice they wake far less reliably on an Echo Dot 2 than Amazon's engine.

- Built-in: **Okay Nabu**, **Hey Jarvis**, **Hey Mycroft**.
- **Your own model:** select a `.tflite` and its `.json` together. For example, the "Hey Hermes" model from hermes-voice (non-commercial licence):
  - [hey_hermes.tflite](https://github.com/majorminorlabs/hermes-voice/raw/main/wakeword/model/hey_hermes.tflite)
  - and its `.json` in the same folder of their repository.

  Start with a threshold of **0.92**. Raise it if it wakes by itself, lower it if it doesn't hear you.

You can train your own phrase with the [hermes-voice wake-word pipeline](https://github.com/majorminorlabs/hermes-voice/tree/main/wakeword). Plan for about 30 GB of disk and several hours. It generates English speech samples, so non-English names work less well.

## Let the bridge select it

Set `WAKE_WORD` in `.env` to the word's id (`alexa`, `computer`, `hey_jarvis`, `hermes`...). Any part of the id or the name works. The bridge matches it against the wake words installed on the Echo and activates it at start. Leave it empty to keep whatever the settings page selected.

## Tips

- Turn on **End of request** (*normal*) on the same page: the Echo then detects the end of your sentence itself.
- Keep the Echo away from walls and from the TV.
