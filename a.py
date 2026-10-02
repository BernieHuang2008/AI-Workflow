import json

def process(resp):
    resp = resp.replace("```json", "")
    resp = resp.replace("```", "")
    j = json.loads(resp)

    res = """
    <style>
        body {
            font-size: 10pt;  /* 五号字 corresponds to 10pt */
        }
        h1 {
            text-align: center;
            font-size: 1.3em;
        }
        p {
            margin: 3pt;
        }
        .paragraph {
            margin-bottom: 3em;
        }
        .sentence {
            margin-bottom: 1.5em;
        }
        .yuanwen {
            font-family: "宋体";
        }
        .yiwen {
            font-family: "宋体";
            color: red;
        }
        .zhushi {
            font-family: "kaiti";
            color: #008000;
        }
        .background {
            font-family: "kaiti";
            color: #1e50b3;
        }
        a {
            color: inherit;
        }

        .uw {
            font-family: "kaiti";
        }

        .uw.wrong {
            color: red;
            text-decoration: line-through;
        }

        .uw.correct {
            color: green;
        }
    </style>
    """

    for title, article in j:
        res += f"<h1 contenteditable>{title}</h1>"

        for i in range(len(article)):  # sentence
            sent = article[i]
            res += "<div class='sentence'>"
            res += f"<p class='yuanwen'>{sent["origin"]}</p>"
            res += f"<p class='yiwen'>{sent["translation"]}</p>"
            res += "<p class='zhushi'>"
            for word in sent["words"]:
                res += f"<span>【{word[0]}】{word[1]} <span class='uw {"wrong" if word[3] else "correct"}'>（用户：{word[2]}）</span></span><br>"
            res += "</p>"
            res += "</div>"

    return {"html": res}
