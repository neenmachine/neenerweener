---
title: agent harnesses
date: 10/21/2026
tags: engineering
---

originally, i was going to write multiple blog posts talking about aspects of agent harnesses, but it got too out of hand. 

this is going to be a long running post about agent harnesses, and what i've learnt as i continue to work on my own harness.

## my first agent "harness"
a couple months before i started working on this harness, i started to experiment with custom workflows built around claude code and codex.

as a long time tmux user, i opted into just giving my agents tools to interact with each other using tmux commands. this is where i first started playing around with orchestrator/subagent workflows.

eventually i realized tmux windows were too difficult to navigate around, and i lacked customizability. so i spent a couple hours one evening building a simple GUI in rust... except it wasn't that simple. since i was still using native claude code & codex, the GUI would essentially have to spawn a headless claude code session, and then send instructions there.

parsing was a whole other thing. my first approach to displaying agent runs in the rust GUI was to explicitly define UI components for every single type of event that i saw in claude's and codex's JSONL files. this was a pain in the ass. things like asking user questions were a nightmare to try and get around, and i really really tried.

within this GUI, i also had an MCP server launch with very a very minimal set of tools: spawning subagents, sending messages between agents, etc.

this eventually grew as i kept maintaining it and forward fixing a bunch of issues that i found while daily driving the gui.

so after 2 months of daily bug fixes, issues with "harnessing" claude code and codex, and constant crashes, i scrapped it.

## a minimal harness
at the time, i was heavily inspired by the engineering paradigms that inspired the creation of [pi](https://github.com/earendil-works/pi)

the current state of agent harnesses oddly has a lot of similarities to linux distributions: cc & codex are ubuntu, pi is arch linux, and the rest of them are... somewhere in between.

i wanted my harness to contain the minimal set of tools required for my day to day workflows. kind of like tool golf.

### tools
after about ~70k LOC, here is the state of my harness:
#### standard tooling
* `bash`: execute bash commands
* `edit`: edit file
* `write`: write a new file
* `read`: read a file
* `load_skill`: load a skill
* `fetch`: fetch webpage source
* `websearch`: natural language web search
* `todo`: simple CRUD tool for agent managed task lists
* `tasks`: standard operations for tasks (`input`, `output`, `run_bg`)
* `subagent`: `spawn`, `cancel`, `output`, `send`, `status`
* MCP support

### subagent profiles
something that ive added to my harness is subagent profiles, which define tool scope, handoff prompts, default models, etc. 

for example, i have an oncall profile that just polls every 10 minutes for any bugs/alerts and raises them to the orchestrator. the orchestrator can then choose whether or not to act on these issues.

### attention
i like not being in the loop all the time. i prefer for agents to do a lot of work autonomously, and only ask questions that require a human decision.

to know when an agent needs my opinion, agents ask me blocking questions through an `request_attention` tool, which i can then check with `/decisions` in the TUI.

![decision example](/images/decisions.png)

if i need more context associated with a question, i can choose to chat with a fork of the main orchestrator run from a checkpoint, which is created when the orchestrator initially calls the `request_attention` tool

### projects
something that i've also found useful is linking projects to active orchestrator runs.

in my day to day workflows, its highly uncommon that i have 2 orchestrators working on the same project. 

this allows me to define a couple things:
* a method of communication between different project orchestrators
* project specific system prompts & subagent profiles

#### p2p communication
its incredibly nice to have orchestrators for different projects interact with each other. most of what i work on is somehow correlated to each other (a lot of it revolves around my harness).

when building things like my messaging agent (will talk about this later) its useful to have my messaging orchestrator request features from my harness orchestrator, so it doesnt have to step outside of its own bounds.

research tasks for any project are sent to a research worker. this orchestrator does pretty much all of my web searches and online research for me, and compiles them into a knowledge vault.

ive found this structure to be really helpful in structuring work so that at one time, agents in a project are ONLY working on that project.

## `ween`: my messaging agent
one of the nicest things about having your own harness is the surface of application, especially in a world where every company is essentially building "harnesses for X/Y/Z"

while these companies are distributing and hardening these harnesses to be scaled to thousands (or even millions) of daily active users, rebuilding parts of their project i find interesting only has to serve me.

a very particular instance of this is messaging services, like instinct, which are at its core, just forks of other traditional, very strong performing harnesses (like pi, hermes, etc) but the engineering process is so much more extensive because they HAVE to care about infra + scaling.

if i wanted to build a texting agent, i wouldn't have to care about scale, or computer, or storage limits. i don't need to worry about whether or not shipping a new feature could quadruple the llm bill. privacy isn't a huge concern either because i'm very close with the only user of the product: me.

![lol](/images/ofc.webp)

the great thing about this era of software engineering is that 99% of companies that i find very cool mostly revolve around building really really good harnesses for a specific task.

whenever i have a free weekend, i try to spend a couple hours throwing together a similar product, on top of my own harness, and just have it live on my home lab.

traditionally, i am not a big fan of messaging agents. it comes down to two things:
1. theyre too chatty
2. they lack user context

i dont always have the energy to tell instinct that the flight ticket that i purchased under my name, sent to my email, was in fact me and not someone else.

a little bit ago, i found this messaging agent called kat, which i played around with by getting it to send me papers about the latest compaction/memory algorithms in harness engineering every couple days.

it was really really good at this, and i think using kat allowed me to realize how valuable messaging agents could be if you could just strip down the noise.

### setting it up
this was surprisingly a lot easier than i thought.

[link](https://linqapp.com/) provides free numbers, so setting up a stock messaging agent just requires giving the agent a tool to text you + setting up a webhook for events on inbound texts.

thats it. it took me 15 minutes of set up (which was mostly opus) and i could just text an agent running on my laptop at home.

however, building a smoother messaging agent requires some work.

### personalization
at the end of the day, messaging agents fall short (imo) because theyre shipping one generic agent, with the ability to "learn" and adapt.

personalization becomes crazy simple if youre building for yourself. 

1. creating a decent persona becomes stupid easy: "talk like this", "do X if Y happens, otherwise do Z"
2. **automation becomes simple**
   * checking in on my agents while out and about is simple
   * i dont even think about organizing my calendar for the week (or the month)
   * reviewing agent traces and iterating on the harness happens instantaneously

every night, my main work laptop sends agent traces to my personal laptop. i've set up very strict local judge to autonomously iterate, and improve failure points within my hours of usage during the day.

when i wake up, i get a couple of messages:
* bug X (which i don't remember encountering) was fixed
* thing Y happened overnight
* person Z emailed you, heres a drafted response

there is so much more to come. i need to find a better way to organize these blogs specifically about harness engineering.
