# Zunkey Klein Story

## 利用前にお読みください

**ソースとリスクを理解したうえで、自己責任で利用・改変してください。AIに読み込ませる場合も、この注意書きと [SECURITY.md](SECURITY.md) を必ず一緒に渡してください。**

- GeminiのAPIキー `nb_api_key` は通常設定と同じ保存対象です。設定JSON・プリセットに平文で含まれます。入力欄の伏せ字は保存データの暗号化を意味しません。
- 設定保存時には既存ファイルの `.json.bak` も作られます。既定の設定JSONや `settings_presets/` はGitの除外対象ですが、`.json.bak` と任意の名前で保存した設定ファイルは必ずしも除外されません。
- 設定・プリセット・バックアップを共有、コミット、フォルダごと再配布する前にAPIキーと私的なプロンプト・参照画像パスが含まれないことを確認してください。元のキーを消しても、バックアップのキーは消えません。
- Gemini連携では選択した画像とプロンプトが外部APIに送信されます。ローカルモデルによる生成と外部API利用を混同しないでください。

これらは未修正の挙動です。注意書きの追加によってコードを修正したわけではありません。AI・自動化ツール向けの詳細は [AGENTS.md](AGENTS.md) と [SECURITY.md](SECURITY.md) を参照してください。


FLUX.2 Klein 4B モデルを使った画像生成GUIアプリケーション（Tkinter）のソースコードです。

**このリポジトリは「そのまま使う完成品」ではなく、AIに読ませてフォーク・改変するためのコードベースです。**
ソースを読める人、自己責任で使える人を対象としています。

## このコードで何ができるか

プロンプトに基づいてキャラクター参照画像付きのストーリー画像を一括生成するGUIアプリです。以下の実装パターンが含まれています。

- **FLUX.2 Klein 4B のパイプライン自動検出** — `Flux2KleinPipeline` / `FluxPipeline` / `DiffusionPipeline` を実行時に signature を見て自動分岐
- **マルチ参照画像の動的バインディング** — `images`, `reference_images`, `input_images` など環境ごとに異なる引数名を自動判定
- **キャラクター名のプロンプト自動置換** — 参照スロットに登録した名前を `[imageN]` に変換
- **ハイブリッド Inpaint** — 画像に直接ダメージペイント → Kleinが破損を認識して修復 → 差分から自動マスク生成 → ソフト合成で修復部分だけ反映
- **Gemini Image API 連携** — REST API 直叩きによる Refine / Edit / Generate（NanoBanana Pro タブ）
- **Tkinter GUI設計パターン** — Notebook タブ、自動保存、プリセット管理、スレッド安全なログ、キーボードショートカット

## コード構造（1ファイル・約4,300行）

意図的に単一ファイル構成にしています。AIに全体を一度に読ませやすくするためです。

```
zunkey_klein_story.py
├── ToolTip (73行〜)          … Tkinter ツールチップヘルパー
├── ユーティリティ関数 (121行〜)  … パス管理、依存関係自動インストール、モデル検出
├── FluxKleinEngine (320行〜)  … FLUX.2 Klein モデルのロード・推論エンジン
├── PromptResolver (558行〜)   … プロンプト解析・キャラ名置換
├── OutputBrowser (619行〜)    … 出力画像のナビゲーション
└── ZunkeyKleinStoryApp (656行〜) … メインGUI（3,650行）
    ├── UI構築 (711〜1080行)
    ├── Inpaintワークフロー (1286〜1920行)
    ├── NanoBanana Pro / Gemini API (1413〜1920行)
    ├── 生成・修正ロジック (2146〜3349行)
    ├── プレビュー・ナビゲーション (2635〜2910行)
    └── 設定・プリセット管理 (3861〜4301行)
```

## AIに読ませてフォークする場合のヒント

このコードをAI（Claude、ChatGPT等）に渡して改変する際の参考情報です。

**そのまま使えるプロンプト例：**

```
このPythonコードを読んで、コード構造を理解してください。
その上で、[やりたいこと] に改変してください。
```

**フォーク・改変のアイデア：**

- `FluxKleinEngine` を別のモデル（SDXL、SD3等）に差し替える
- `PromptResolver` のキャラ名置換ロジックを拡張してLoRA切り替えに対応する
- Inpaint のハイブリッド手法（直接ダメージ + 自動マスク合成）を自分のパイプラインに移植する
- Gemini API 連携部分だけ抜き出して別ツールにする
- Tkinter を別のGUIフレームワーク（PyQt、Gradio等）に置き換える

**注意点：**

- `_build_kwargs()` メソッドが Klein の引数名差異を吸収する中核ロジックです
- Inpaint は「Kleinに破損画像を渡す」＋「出力後に差分マスクで合成」の2段構成です
- Gemini API は `google-generativeai` ライブラリではなく REST API を直接呼んでいます

## 動作環境（参考）

- Python 3.10+
- NVIDIA GPU（CUDA 対応）推奨
- Windows / Linux
- 依存パッケージ: `requirements.txt` 参照

## ライセンス

MIT License - 自由にフォーク・改変・再配布できます。詳細は [LICENSE](LICENSE) を参照してください。

## 作者

榊正宗 ([@megamarsun](https://x.com/megamarsun))
