import gradio as gr


def predict(text):
    return f"You said: {text}"


demo = gr.Interface(fn=predict, inputs="text", outputs="text", title="My Gradio App")

demo.launch(server_name="0.0.0.0", server_port=7860)
