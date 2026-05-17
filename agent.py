import os
import subprocess
import asyncio
from telegram.ext import Application, MessageHandler, CommandHandler, filters
import anthropic

# Config
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["YOUR_CHAT_ID"])
REPO_PATH = os.path.expanduser("~/your-android-repo")
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# Security check
def is_authorized(update):
    return update.message.chat_id == CHAT_ID

# Run shell commands
def run(cmd, cwd=REPO_PATH):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    return result.stdout + result.stderr

# Plan feature with Claude
def plan_feature(feature_description):
    context = run("find . -name '*.kt' -not -path '*/build/*' | head -50")
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": f"""
You are a senior Android architect working on a multi-module IPTV app called Channel Cast.

Project modules:
- app/ - Main app module
- data-* - Data layer (storage, network, repository, prefs)
- ui-* - UI layer (features, core, models)
- channel-health-monitor/
- proxy-health-monitor/

Project files sample:
{context}

Feature request: {feature_description}

Produce:
1. Branch name (feature/xxx)
2. Files to create/modify
3. Step by step implementation plan
4. Ready-to-use Codex prompt
            """
        }]
    )
    return response.content[0].text

# Implement with Codex
def implement(plan, branch_name):
    run("git checkout main")
    run("git pull origin main")
    run(f"git checkout -b {branch_name}")
    run(f'codex "{plan}"')

# Push to GitHub  
def push(branch_name, feature_name):
    run("git add .")
    run(f'git commit -m "feat: {feature_name}"')
    run(f"git push origin {branch_name}")

# Commands
async def start(update, context):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "👋 Channel Cast Agent ready!\n\n"
        "Commands:\n"
        "/implement <feature> - Plan and implement a feature\n"
        "/plan <feature> - Plan only, no implementation\n"
        "/branches - List all branches\n"
        "/status - Git status\n"
    )

async def branches(update, context):
    if not is_authorized(update):
        return
    result = run("git branch -a")
    await update.message.reply_text(f"🌿 Branches:\n{result}")

async def status(update, context):
    if not is_authorized(update):
        return
    result = run("git status")
    await update.message.reply_text(f"📊 Status:\n{result}")

async def plan(update, context):
    if not is_authorized(update):
        return
    feature = " ".join(context.args)
    if not feature:
        await update.message.reply_text("Usage: /plan <feature description>")
        return

    await update.message.reply_text("🧠 Planning with Claude...")
    plan_text = plan_feature(feature)
    await update.message.reply_text(f"📋 Plan:\n{plan_text}")

async def implement_cmd(update, context):
    if not is_authorized(update):
        return
    feature = " ".join(context.args)
    if not feature:
        await update.message.reply_text("Usage: /implement <feature description>")
        return

    await update.message.reply_text("🧠 Planning with Claude...")
    plan_text = plan_feature(feature)
    await update.message.reply_text(f"📋 Plan:\n{plan_text}")

    await update.message.reply_text("⚙️ Implementing with Codex...")
    branch_name = f"feature/{feature.lower().replace(' ', '-')}"
    implement(plan_text, branch_name)

    await update.message.reply_text("🚀 Pushing to GitHub...")
    push(branch_name, feature)

    await update.message.reply_text(f"✅ Done!\nBranch: {branch_name}\nPull it locally to test.")

# Run bot
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("implement", implement_cmd))
    app.add_handler(CommandHandler("branches", branches))
    app.add_handler(CommandHandler("status", status))
    print("🤖 Agent running...")
    app.run_polling()

if __name__ == "__main__":
    main()
