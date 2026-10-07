def build_report(items):
    r = []
    t = 0
    for i in items:
        if i["qty"] > 0:
            if i["price"] > 0:
                l = i["name"] + ": " + str(i["qty"]) + " x " + str("%.2f" % i["price"]) + " = " + str("%.2f" % (i["qty"] * i["price"]))
                r.append(l)
                t = t + i["qty"] * i["price"]
            else:
                pass
        else:
            pass
    r.append("TOTAL: " + str("%.2f" % t))
    return "\n".join(r)
