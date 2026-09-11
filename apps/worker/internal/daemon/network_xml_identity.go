package daemon

import (
	"bytes"
	"encoding/xml"
	"errors"
	"io"
	"reflect"
	"slices"
	"strings"
)

type networkXMLNode struct {
	Name     xml.Name
	Attrs    []xml.Attr
	Text     string
	Children []*networkXMLNode
}

// Preserve every non-namespace attribute and element for an exact structural
// comparison. Unknown fields, duplicate attributes and multiple roots cannot
// disappear through encoding/xml's usual last-field-wins struct decoding.
func parseNetworkXML(data []byte, root string) (*networkXMLNode, error) {
	if len(data) > 256<<10 {
		return nil, errVMCleanup
	}
	decoder := xml.NewDecoder(bytes.NewReader(data))
	var result *networkXMLNode
	var stack []*networkXMLNode
	for {
		token, err := decoder.Token()
		if errors.Is(err, io.EOF) {
			if result == nil || len(stack) != 0 {
				return nil, errVMCleanup
			}
			return result, nil
		}
		if err != nil {
			return nil, errVMCleanup
		}
		switch value := token.(type) {
		case xml.StartElement:
			if len(stack) >= 64 || len(stack) == 0 && (result != nil || value.Name != (xml.Name{Local: root})) {
				return nil, errVMCleanup
			}
			node := &networkXMLNode{Name: value.Name}
			seen := map[xml.Name]bool{}
			for _, attr := range value.Attr {
				if seen[attr.Name] {
					return nil, errVMCleanup
				}
				seen[attr.Name] = true
				if attr.Name.Space != "xmlns" && attr.Name != (xml.Name{Local: "xmlns"}) {
					node.Attrs = append(node.Attrs, attr)
				}
			}
			slices.SortFunc(node.Attrs, func(a, b xml.Attr) int {
				return strings.Compare(a.Name.Space+":"+a.Name.Local, b.Name.Space+":"+b.Name.Local)
			})
			if len(stack) == 0 {
				result = node
			} else {
				parent := stack[len(stack)-1]
				parent.Children = append(parent.Children, node)
			}
			stack = append(stack, node)
		case xml.EndElement:
			stack = stack[:len(stack)-1]
		case xml.CharData:
			text := strings.TrimSpace(string(value))
			if text != "" {
				if len(stack) == 0 {
					return nil, errVMCleanup
				}
				stack[len(stack)-1].Text += text
			}
		case xml.Directive, xml.ProcInst:
			return nil, errVMCleanup
		}
	}
}

func (node *networkXMLNode) attr(name string) string {
	for _, attr := range node.Attrs {
		if attr.Name == (xml.Name{Local: name}) {
			return attr.Value
		}
	}
	return ""
}

func (node *networkXMLNode) child(name string) *networkXMLNode {
	var result *networkXMLNode
	for _, child := range node.Children {
		if child.Name == (xml.Name{Local: name}) {
			if result != nil {
				return nil
			}
			result = child
		}
	}
	return result
}

func (node *networkXMLNode) references(values ...string) bool {
	if slices.Contains(values, node.Text) {
		return true
	}
	for _, attr := range node.Attrs {
		if slices.Contains(values, attr.Value) {
			return true
		}
	}
	for _, child := range node.Children {
		if child.references(values...) {
			return true
		}
	}
	return false
}

func normalizeNetworkXML(node *networkXMLNode) {
	// Only documented defaults that libvirt may omit are equivalent. Never
	// discard a nonzero connection count, extra route, port or firewall option.
	defaults := map[string]map[string]string{
		"network": {"ipv6": "no", "trustGuestRxFilters": "no", "connections": "0"},
		"dns":     {"enable": "yes"}, "ip": {"family": "ipv4"},
	}
	node.Attrs = slices.DeleteFunc(node.Attrs, func(attr xml.Attr) bool {
		return attr.Name.Space == "" && defaults[node.Name.Local][attr.Name.Local] == attr.Value && attr.Value != ""
	})
	if len(node.Attrs) == 0 {
		node.Attrs = nil
	}
	for _, child := range node.Children {
		normalizeNetworkXML(child)
	}
	// libvirt 10 materializes its default NAT source-port range only in the
	// active view. Accept exactly that default, never additional NAT addresses,
	// IPv6 options or a changed port range.
	if node.Name == (xml.Name{Local: "forward"}) && node.attr("mode") == "nat" && len(node.Attrs) == 1 &&
		node.Text == "" && len(node.Children) == 1 {
		nat := node.Children[0]
		if nat.Name == (xml.Name{Local: "nat"}) && len(nat.Attrs) == 0 && nat.Text == "" && len(nat.Children) == 1 {
			port := nat.Children[0]
			if port.Name == (xml.Name{Local: "port"}) && len(port.Attrs) == 2 && port.attr("start") == "1024" &&
				port.attr("end") == "65535" && port.Text == "" && len(port.Children) == 0 {
				node.Children = nil
			}
		}
	}
}

func matchesNetworkXML(data, expected []byte, root string) bool {
	live, err := parseNetworkXML(data, root)
	if err != nil {
		return false
	}
	want, err := parseNetworkXML(expected, root)
	if err != nil {
		return false
	}
	if root == "network" {
		normalizeNetworkXML(live)
		normalizeNetworkXML(want)
	}
	return reflect.DeepEqual(live, want)
}
