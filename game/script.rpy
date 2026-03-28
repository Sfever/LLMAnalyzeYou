image blue smile = "images/BLUE/smile.png"

define muse = Character("Muse")
define player = Character("You")
define used_character=""

transform fit_screen:
    xalign 0.5
    yalign 0.5
    xsize config.screen_width
    ysize config.screen_height
    fit "contain"

label start:
    scene black
    with dissolve
    init python:
        if not store.llm_api_key:
            renpy.notify("Please set an API key to continue")

    $ llm_clear_history()
    $ llm_add_system_message("You are Muse. Reply briefly, stay in character, and use the change_expression tool whenever your visible emotion changes.")


    label llm_chat_loop:
        $ user_message = renpy.input("Say something to Muse.").strip()

        if not user_message:
            muse "Give me something to respond to."
            jump llm_chat_loop

        if user_message.lower() in ("quit", "exit"):
            muse "Ending the conversation."
            return

        $ llm_add_user_message(user_message)
        player "[user_message]"
        $ llm_stream_reply("Muse", llm_copy_history())

        if llm_stream_error:
            "Backend error: [llm_stream_error]"
            return

        jump llm_chat_loop

    return  
