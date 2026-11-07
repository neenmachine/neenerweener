"""Image delivery attributes for trusted Markdown renders."""


def add_image_loading(state) -> None:
    mode = state.env.get("image_loading")
    if mode not in {"post", "readme"}:
        return
    first = True
    for token in state.tokens:
        for child in token.children or []:
            if child.type != "image":
                continue
            if mode == "post" and first:
                child.attrSet("decoding", "async")
            else:
                child.attrSet("loading", "lazy")
                child.attrSet("decoding", "async")
            first = False
