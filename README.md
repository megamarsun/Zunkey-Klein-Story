# Zunkey Klein Story

FLUX.2 Klein 4B モデルを使った画像生成GUIアプリケーション（Tkinter）です。
プロンプトに基づいてキャラクター参照画像付きのストーリー画像を一括生成できます。

## 主な機能

- **プロンプト一括生成** — 1行1プロンプトで複数画像をまとめて生成
- **キャラクター参照画像** — 最大8枚の参照画像スロット。プロンプト中のキャラ名を自動で `[imageN]` に置換
- **共通プロンプト** — Base / Story / Strict の3モードで共通指示を付加
- **インタラクティブ修正** — 生成画像を参照として再投入し、破綻部分を修正
- **ハイブリッド Inpaint** — 画像に直接ペイント → Klein が破損を認識 → 自動マスク合成で修復部分だけ反映
- **NanoBanana Pro** — Gemini Image API 連携（Refine / Edit / Generate）
- **画像管理** — G/S/B/U マーク、キーボードショートカット、マーク別エクスポート
- **設定・プリセット** — JSON 自動保存、名前付きプリセット管理

## 動作環境

- Python 3.10+
- NVIDIA GPU（CUDA 対応）推奨
- Windows / Linux

## インストール

```bash
git clone https://github.com/megamarsun/Zunkey-Klein-Story.git
cd Zunkey-Klein-Story
pip install -r requirements.txt
```

## 使い方

```bash
python zunkey_klein_story.py
```

初回起動時に FLUX.2 Klein 4B モデルが自動ダウンロードされます（HuggingFace Hub から取得）。
手動で配置する場合は `TMP/models/FLUX.2-klein-4B` にモデルを置いてください。

## キーボードショートカット

| キー | 機能 |
|------|------|
| `←` `→` | 前/次の画像 |
| `G` `S` `B` `U` | Good / Star / Bad / Unmarked マーク |
| `E` | プロンプト表示切替 |
| `D` | 画像削除 |
| `Home` `End` | 最初/最後の画像へ |

## NanoBanana Pro（Gemini API）

Gemini Image API を使った画像の高品質化・編集・生成機能です。
利用には [Google AI Studio](https://aistudio.google.com/apikey) で API キーを取得してください。

## ライセンス

MIT License - 詳細は [LICENSE](LICENSE) を参照してください。

## 作者

榊正宗 ([@megamarsun](https://x.com/megamarsun))
