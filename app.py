"""
GRE Mock Test Platform — application entry point.
"""
import wx
from main_frame import MainFrame


class GREMockApp(wx.App):
    def OnInit(self):
        frame = MainFrame()
        frame.Show()
        return True


def main():
    app = GREMockApp()
    app.MainLoop()


if __name__ == "__main__":
    main()
